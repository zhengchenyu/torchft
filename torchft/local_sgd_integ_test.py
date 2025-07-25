import copy
import logging
import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import timedelta
from sys import platform
from typing import Any, Dict
from unittest import TestCase

import torch
from parameterized import parameterized
from torch import nn, optim
from torch.distributed.tensor import DTensor, Replicate

from torchft._torchft import LighthouseServer
from torchft.device_mesh import ft_init_device_mesh
from torchft.local_sgd import DiLoCo, LocalSGD
from torchft.manager import Manager
from torchft.manager_integ_test import FailureInjector, MyModel, Runner
from torchft.process_group import ProcessGroupBabyNCCL, ProcessGroupGloo

logger: logging.Logger = logging.getLogger(__name__)


def local_sgd_train_loop(
    rank: int,
    store_port: int,
    device: torch.device,
    runner: Runner,
) -> Dict[str, Dict[str, object]]:
    with ExitStack() as stack:

        def load_state_dict(state_dict: Dict[str, Dict[str, object]]) -> None:
            m.load_state_dict(state_dict["model"])
            optimizer.load_state_dict(state_dict["optim"])

        def state_dict() -> Dict[str, Dict[str, object]]:
            return {
                "model": m.state_dict(),
                "optim": optimizer.state_dict(),
            }

        print(f"worker {runner.replica_id=} {rank=} {runner.world_size=} starting")

        if device.type == "cuda":
            pg = ProcessGroupBabyNCCL()
        else:
            pg = ProcessGroupGloo()
        manager = Manager(
            pg=pg,
            min_replica_size=2,
            load_state_dict=load_state_dict,
            state_dict=state_dict,
            replica_id=str(runner.replica_id),
            store_addr="localhost",
            store_port=store_port,
            rank=rank,
            world_size=runner.world_size,
            lighthouse_addr=runner.lighthouse_address,
            port=19530 + runner.replica_id,
            timeout=timedelta(seconds=10),
            # pyre-fixme[6]: Incompatible parameter type
            **runner.manager_args,
        )
        stack.callback(lambda: manager.shutdown(wait=False))

        m: nn.Module = MyModel().to(device)

        optimizer: optim.Optimizer = optim.Adam(m.parameters())
        criterion = nn.CrossEntropyLoss()

        with LocalSGD(manager, m, optimizer, sync_every=2) as local_sgd:
            while True:
                inputs = torch.rand(2, 3).to(device)
                labels = torch.randint(4, (2,)).to(device)

                optimizer.zero_grad()
                out = m(inputs)
                loss = criterion(out, labels)
                loss.backward()

                optimizer.step()

                if manager.current_step() >= 4:
                    break

                runner.failure_injector.check(rank, manager.current_step())

        # return state_dict so we can check consistency
        return state_dict()
    return {}


def diloco_train_loop(
    rank: int,
    store_port: int,
    device: torch.device,
    runner: Runner,
) -> Dict[str, Dict[str, object]]:
    with ExitStack() as stack:
        # Declare the model and optimizers
        m: nn.Module = MyModel(2, 3)
        model_state_dict: Dict[str, Any] = runner.train_loop_args["model_state_dict"]
        m.load_state_dict(model_state_dict)
        m.to(device)

        # Setup optimizers
        inner_optimizer: optim.Optimizer = torch.optim.AdamW(
            m.parameters(), lr=4e-4, weight_decay=0.1, betas=(0.9, 0.95)
        )
        outer_optimizer: optim.Optimizer = torch.optim.SGD(
            m.parameters(), lr=0.7, momentum=0.9, nesterov=True
        )

        # pyre-ignore[53]
        def load_state_dict(state_dict: Dict[str, Dict[str, object]]) -> None:
            m.load_state_dict(state_dict["model"])
            m.to(device)
            diloco._fragments[0].original_parameters = state_dict["original_params"]
            for name in diloco._fragments[0].original_parameters.keys():
                diloco._fragments[0].original_parameters[name] = (
                    diloco._fragments[0].original_parameters[name].to(device)
                )
            inner_optimizer.load_state_dict(state_dict["inner_optim"])
            outer_optimizer.load_state_dict(state_dict["outer_optim"])

        def state_dict() -> Dict[str, Dict[str, object]]:  # pyre-ignore[53]
            return {
                "model": m.state_dict(),
                "original_params": diloco._fragments[0].original_parameters,
                "inner_optim": inner_optimizer.state_dict(),
                "outer_optim": outer_optimizer.state_dict(),
            }

        print(f"worker {runner.replica_id=} {rank=} {runner.world_size=} starting")

        if device.type == "cuda":
            pg = ProcessGroupBabyNCCL()
        else:
            pg = ProcessGroupGloo(timeout=timedelta(seconds=10))
        manager = Manager(
            pg=pg,
            min_replica_size=2,
            use_async_quorum=False,
            load_state_dict=load_state_dict,
            state_dict=state_dict,
            replica_id=str(runner.replica_id),
            store_addr="localhost",
            store_port=store_port,
            rank=rank,
            world_size=runner.world_size,
            lighthouse_addr=runner.lighthouse_address,
            port=19530 + runner.replica_id,
            connect_timeout=timedelta(seconds=10),
            quorum_timeout=timedelta(seconds=10),
            timeout=timedelta(seconds=10),
            # pyre-fixme[6]: Incompatible parameter type
            **runner.manager_args,
        )
        stack.callback(manager.shutdown)
        # initialize default group for device mesh to work
        if not torch.distributed.is_initialized():
            # TODO: remove this try-except once pytorch is updated to 2.8.0 and can use localhost:0
            try:
                torch.distributed.init_process_group(
                    init_method="tcp://localhost:0",
                    rank=rank,
                    world_size=runner.world_size,
                )
            except ValueError:
                os.environ["MASTER_ADDR"] = "localhost"
                os.environ["MASTER_PORT"] = "0"
                os.environ["WORLD_SIZE"] = str(runner.world_size)
                os.environ["RANK"] = str(rank)

        device_type = device.type
        ft_device_mesh = ft_init_device_mesh(
            device_type=device_type,
            mesh_shape=(runner.world_size, 1),
            mesh_dim_names=("replicate", "none"),
            replicate_dim=0,
            manager=manager,
        )
        for layer in m.layers:
            if isinstance(layer, nn.Linear):
                for param in layer.parameters():
                    param = DTensor.from_local(
                        param,
                        device_mesh=ft_device_mesh,
                    )

        criterion = nn.CrossEntropyLoss()
        all_state_dicts = {}
        with DiLoCo(
            manager,
            [m],
            inner_optimizer,
            outer_optimizer,
            backup_device=device,
            sync_every=2,
        ) as diloco:
            while True:
                manager_curr_step = manager.current_step()
                if manager_curr_step not in all_state_dicts:
                    all_state_dicts[manager_curr_step] = copy.deepcopy(state_dict())
                batch_size = 1
                inputs = m.get_rand_inputs(batch_size, device=device)
                labels = m.get_rand_labels(batch_size, device=device)

                out = m(inputs)
                loss = criterion(out, labels)

                inner_optimizer.zero_grad()
                loss.backward()
                inner_optimizer.step()

                # after 4 model updates then break
                if manager.current_step() >= 4:
                    break

                runner.failure_injector.check(rank, manager.current_step())

        # return state_dict so we can check consistency
        return all_state_dicts
    return {}


class LocalSGDIntegTest(TestCase):
    # TODO: race condition due to using NCCL in threads causes manager allreduce to sometimes not be correct
    # Because of that the test is disabled for cuda
    @parameterized.expand(
        [
            # (True,),
            (False,),
        ]
    )
    def test_local_sgd_recovery(self, use_cuda: bool) -> None:
        # Skip the test if use_cuda is True and there are not enough GPUs
        if use_cuda and torch.cuda.device_count() < 2:
            self.skipTest("Not enough GPUs for CUDA test")

        lighthouse = LighthouseServer(
            bind="[::]:0",
            min_replicas=2,
        )
        num_replicas = 2
        futures = []

        failure_injectors = [
            FailureInjector(),
            FailureInjector().fail_at(0, 2),
        ]

        with ThreadPoolExecutor(max_workers=num_replicas) as executor:
            for replica_id, failure_injector in zip(
                range(num_replicas), failure_injectors
            ):
                runner = Runner(
                    replica_id=replica_id,
                    num_replicas=num_replicas,
                    lighthouse_address=lighthouse.address(),
                    failure_injector=failure_injector,
                    train_loop=local_sgd_train_loop,
                    use_cuda=use_cuda,
                    manager_args={
                        "use_async_quorum": False,
                    },
                )
                futures.append(executor.submit(runner.run_replica))

            state_dicts = []

            for fut in as_completed(futures):
                try:
                    state_dicts.append(fut.result())
                except Exception as e:
                    print(e)
                    raise

        lighthouse.shutdown()

        for state_dict in state_dicts:
            # LocalSGD only guarantees that the model is consistent across
            # replicas but uses separate optimizer states.
            torch.testing.assert_close(
                state_dict[0]["model"], state_dicts[0][0]["model"], check_device=False
            )

        self.assertEqual(failure_injectors[1].count, 1)

    @parameterized.expand(
        [
            # (True,),
            (False,),
        ]
    )
    def test_diloco_healthy(self, use_cuda: bool) -> None:
        # Skip the test if use_cuda is True and there are not enough GPUs
        if use_cuda and torch.cuda.device_count() < 2:
            self.skipTest("Not enough GPUs for CUDA test")

        lighthouse = LighthouseServer(bind="[::]:0", min_replicas=2)
        num_replicas = 2
        futures = []

        torch.manual_seed(42)
        # Initialize the model so we can pass in the state_dict
        m: nn.Module = MyModel(2, 3)

        with ThreadPoolExecutor(max_workers=num_replicas) as executor:
            for replica_id in range(num_replicas):
                failure_injector = FailureInjector()
                runner = Runner(
                    replica_id=replica_id,
                    num_replicas=num_replicas,
                    lighthouse_address=lighthouse.address(),
                    failure_injector=failure_injector,
                    train_loop=diloco_train_loop,
                    use_cuda=use_cuda,
                    train_loop_args={
                        "model_state_dict": m.state_dict(),
                    },
                )
                futures.append(executor.submit(runner.run_replica))

            state_dicts = []
            for fut in as_completed(futures):
                try:
                    state_dicts.append(fut.result()[0])
                except Exception as e:
                    print(e, flush=True)
                    traceback.print_exc()
                    raise

        lighthouse.shutdown()

        rep0, rep1 = state_dicts
        for step, state_dict in rep1.items():
            # inner optimizer will be different, outer optimizer and model should be the same
            torch.testing.assert_close(
                state_dict["model"],
                rep0[step]["model"],
                check_device=False,
            )
            torch.testing.assert_close(
                state_dict["outer_optim"],
                rep0[step]["outer_optim"],
                check_device=False,
            )

    @parameterized.expand(
        [
            # (True,),
            (False,),
        ]
    )
    def test_diloco_recovery(self, use_cuda: bool) -> None:
        # Skip the test if use_cuda is True and there are not enough GPUs
        if use_cuda and torch.cuda.device_count() < 2:
            self.skipTest("Not enough GPUs for CUDA test")

        if platform == "darwin":
            # TODO: This is likely because of Gloo not releasing GIL.
            # Fix in: https://github.com/pytorch/pytorch/pull/154976
            # Once this makes it to a stable package, we can re-enable this test.
            self.skipTest("Known issue in Gloo")

        lighthouse = LighthouseServer(
            bind="[::]:0",
            min_replicas=2,
        )
        num_replicas = 2
        futures = []

        failure_injectors = [
            FailureInjector(),
            FailureInjector().fail_at(0, 2),
        ]

        torch.manual_seed(42)
        # Initialize the model so we can pass in the state_dict
        m: nn.Module = MyModel(2, 3)

        with ThreadPoolExecutor(max_workers=num_replicas) as executor:
            for replica_id, failure_injector in zip(
                range(num_replicas), failure_injectors
            ):
                runner = Runner(
                    replica_id=replica_id,
                    num_replicas=num_replicas,
                    lighthouse_address=lighthouse.address(),
                    failure_injector=failure_injector,
                    train_loop=diloco_train_loop,
                    train_loop_args={
                        "model_state_dict": m.state_dict(),
                    },
                )
                futures.append(executor.submit(runner.run_replica))

            state_dicts = []

            for fut in as_completed(futures):
                try:
                    state_dicts.append(fut.result()[0])
                except Exception as e:
                    print(e)
                    raise

        lighthouse.shutdown()

        rep0, rep1 = state_dicts

        for step in rep0.keys():
            # Inner optimizer and local model parameters will be different e.g.
            # with 2 replicas r1 and r2, we sync every 2 steps
            #
            # - Manager Step 1
            #   - Step 1: r1 and r2 step
            #   - Step 2: r1 and r2 step, sync the model, quorum succeeds
            # - Manager Step 2
            #   - Step 1: r1 steps but r2 fails
            #   - Step 2:
            #     - r1 steps, sync fails because r2 is down
            #     - r1 recovers r2 from the model state at this step
            #       that is different from the model for r1 at the beginning
            #       of step Manager Step 2
            #
            # Outer optimizer and global model should be the same

            torch.testing.assert_close(
                rep1[step]["original_params"],
                rep0[step]["original_params"],
                check_device=False,
            )
            torch.testing.assert_close(
                rep1[step]["outer_optim"],
                rep0[step]["outer_optim"],
                check_device=False,
            )
        self.assertEqual(failure_injectors[1].count, 1)
