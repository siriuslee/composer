# Copyright 2021 MosaicML. All Rights Reserved.

from __future__ import annotations

import warnings
from dataclasses import asdict, dataclass
from typing import Optional

import torch.profiler
from torch.profiler.profiler import ProfilerAction

from composer.callbacks.callback_hparams import TorchProfilerHparams
from composer.core import Callback, Logger, State
from composer.core.types import StateDict
from composer.utils.ddp import get_global_rank
from composer.utils.run_directory import get_relative_to_run_directory

_PROFILE_MISSING_ERROR = "The profiler has not been setup. Please call profiler.training_start() before training starts."


@dataclass
class _TorchProfilerState:
    batch_in_epoch: int = 0
    batches_per_epoch: int = 0


class TorchProfiler(Callback):
    """Profile the execution using :class:`torch.profiler.profile`.

    Profiling results are stored in TensorBoard format in the
    :param tensorboard_trace_handler_dir: folder.

    To view profiling results, run:

    ``tensorboard --logdir tensorboard_trace_handler_dir``

    Also see https://pytorch.org/docs/stable/profiler.html.

    Args:
        tensorboard_trace_handler_dir (str): Directory to store trace results.
            Relative to the run_directory. Defaults to `torch_profiler` in the
            run directory.
        tensorboard_use_gzip (bool, optional):
            Whether to use gzip for the trace. Defaults to False.
        record_shapes (bool, optional): Whether to record tensor shapes.
            Defaults to True.
        profile_memory (bool, optional): Whether to profile memory.
            Defaults to False.
        with_stack (bool, optional): Whether to record stack info.
            Defaults to True.
        with_flops (bool, optional): Whether to estimate flops for operators.
            Defaults to True.
        skip (int, optional): Number of batches to skip at epoch start.
            Defaults to 0.
        warmup (int, optional): Number of warmup batches in a cycle.
            Defaults to 1.
        active (int, optional): Number of batches to profile in a cycle.
            Defaults to 5.
        wait (int, optional): Number of batches to skip at the end of each cycle.
            Defaults to 0.
    """

    def __init__(
        self,
        *,
        tensorboard_trace_handler_dir: str = "torch_profiler",
        tensorboard_use_gzip: bool = False,
        record_shapes: bool = True,
        profile_memory: bool = False,
        with_stack: bool = True,
        with_flops: bool = True,
        skip: int = 0,
        warmup: int = 1,
        active: int = 5,
        wait: int = 0,
        repeat: int = 0,
    ) -> None:
        super().__init__()
        self.hparams = TorchProfilerHparams(
            skip=skip,
            warmup=warmup,
            active=active,
            wait=wait,
            tensorboard_trace_handler_dir=get_relative_to_run_directory(tensorboard_trace_handler_dir),
            tensorboard_use_gzip=tensorboard_use_gzip,
            record_shapes=record_shapes,
            profile_memory=profile_memory,
            with_stack=with_stack,
            with_flops=with_flops,
            repeat=repeat,
        )
        self.profiler: Optional[torch.profiler.profile] = None
        self.profiler_state: _TorchProfilerState = _TorchProfilerState()
        self._torch_profiler_scheduler = torch.profiler.profiler.schedule(
            wait=self.hparams.wait,
            warmup=self.hparams.warmup,
            active=self.hparams.active,
            skip_first=self.hparams.skip,
            repeat=self.hparams.repeat,
        )
        try:
            import torch_tb_profiler  # type: ignore
        except ModuleNotFoundError:
            warnings.warn(
                "TorchTBProfilerNotFound: torch_tb_profiler not found. You will not be able to visualize torch profiler results."
                "To visualize, run `pip install torch-tb-profiler`")

    def state_dict(self) -> StateDict:
        return asdict(self.profiler_state)

    def load_state_dict(self, state: StateDict) -> None:
        self.profiler_state = _TorchProfilerState(**state)

    def scheduler_fn(self, profiler_step: int) -> ProfilerAction:
        # Invoked on every batch, at the batch end
        # But, it's called one batch in advance.
        # Wrapping the default scheduling function to deal with epoch boundaries
        # Giving the torch scheduler the batch in the epoch, not the global step

        # adding 1 since this is called before the step is incremented
        next_batch_in_epoch = self.profiler_state.batch_in_epoch + 1
        if profiler_step == 0:
            next_batch_in_epoch = 0
        torch_scheduler_action = self._torch_profiler_scheduler(next_batch_in_epoch)
        if next_batch_in_epoch == self.profiler_state.batches_per_epoch:
            if torch_scheduler_action == ProfilerAction.RECORD:
                # force saving at epoch boundaries
                torch_scheduler_action = ProfilerAction.RECORD_AND_SAVE
        return torch_scheduler_action

    def init(self, state: State, logger: Logger) -> None:
        del state, logger  # unused
        assert self.profiler is None, _PROFILE_MISSING_ERROR
        self.profiler = torch.profiler.profile(
            schedule=self.scheduler_fn,
            on_trace_ready=torch.profiler.tensorboard_trace_handler(
                self.hparams.tensorboard_trace_handler_dir,
                worker_name=str(get_global_rank()),
                use_gzip=self.hparams.tensorboard_use_gzip,
            ),
            activities=None,  # auto-set
            record_shapes=self.hparams.record_shapes,
            profile_memory=self.hparams.profile_memory,
            with_stack=self.hparams.with_stack,
            with_flops=self.hparams.with_flops,
        )
        self.profiler.__enter__()

    def batch_end(self, state: State, logger: Logger) -> None:
        del state, logger  # unused
        assert self.profiler is not None, _PROFILE_MISSING_ERROR
        self.profiler.step()

    def epoch_start(self, state: State, logger: Logger) -> None:
        del logger  # unused
        self.profiler_state.batches_per_epoch = state.steps_per_epoch

    def batch_start(self, state: State, logger: Logger) -> None:
        self.profiler_state.batch_in_epoch = state.batch_idx
        assert self.profiler is not None, _PROFILE_MISSING_ERROR
        logger.metric_batch({"profiler/state": self.profiler.current_action.name})

    def close(self) -> None:
        if self.profiler is not None:
            self.profiler.__exit__(None, None, None)
