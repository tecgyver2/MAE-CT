import logging
import os

import kappaprofiler as kp
import torch
from torch.distributed import broadcast_object_list
from wandb.util import generate_id

from configs.cli_args import parse_run_cli_args
from configs.static_config import StaticConfig
from configs.util import cliarg_or_staticvalue
from distributed.config import barrier, get_rank, get_local_rank, get_world_size, is_managed, is_rank0, is_distributed
from distributed.run import run_single_or_multiprocess, run_managed
from providers.stage_path_provider import StagePathProvider
from train_stage import train_stage
from utils.kappaconfig.util import get_run_hp
from utils.kappaconfig.util import get_stage_hp_list, get_max_batch_sizes_from_cli
from utils.kappaconfig.util import get_stage_ids_from_cli
from utils.logging_util import add_global_handlers, log_from_all_ranks
from utils.pytorch_cuda_timing import cuda_start_event, cuda_end_event
from utils.version_check import check_versions


def main_single(device):
    cli_args = parse_run_cli_args()
    static_config = StaticConfig(uri="static_config.yaml", datasets_were_preloaded=cli_args.datasets_were_preloaded)
    add_global_handlers(log_file_uri=None)
    with log_from_all_ranks():
        logging.info(f"initialized process rank={get_rank()} local_rank={get_local_rank()} pid={os.getpid()}")
    barrier()
    logging.info(f"initialized {get_world_size()} processes")

    # CUDA_LAUNCH_BLOCKING=1 for debugging
    # os.environ["CUDA_LAUNCH_BLOCKING"] = str(1)

    # cudnn
    if cli_args.accelerator == "gpu":
        if cliarg_or_staticvalue(cli_args.cudnn_benchmark, static_config.default_cudnn_benchmark):
            torch.backends.cudnn.benchmark = True
            assert not static_config.default_cudnn_deterministic, "cudnn_benchmark can make things non-deterministic"
        else:
            logging.warning(f"disabled cudnn benchmark")
            if static_config.default_cudnn_deterministic:
                torch.backends.cudnn.deterministic = True
                os.environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"
                logging.warning(f"enabled cudnn deterministic")

    # profiling
    if cli_args.accelerator == "gpu":
        if cliarg_or_staticvalue(cli_args.cuda_profiling, static_config.default_cuda_profiling):
            kp.setup_async(cuda_start_event, cuda_end_event)
            logging.info(f"initialized profiler to call sync cuda")
    else:
        kp.setup_async_as_sync()

    # parse stages
    run_hp = get_run_hp(cli_args.hp)
    stage_names, stage_hp_list = get_stage_hp_list(
        run_hp,
        template_path="yamls/templates",
        testrun=cli_args.testrun,
        minmodelrun=cli_args.minmodelrun,
        mindatarun=cli_args.mindatarun,
        mindurationrun=cli_args.mindurationrun,
    )
    max_batch_sizes = get_max_batch_sizes_from_cli()

    # parse stage_ids from cli (required in case of starting not from the first stage)
    stage_ids = get_stage_ids_from_cli()
    # check that stage_ids actually exist
    for stage_name, stage_id in stage_ids.items():
        stage_path_provider = StagePathProvider(
            output_path=static_config.output_path,
            stage_name=stage_name,
            stage_id=stage_id,
        )
        assert stage_path_provider.stage_output_path_exists, \
            f"invalid stage_name ({stage_name}) or invalid stage_id ({stage_id})"

    # TODO the logging for this is not ideal
    for i, (stage_name, stage_hp) in enumerate(zip(stage_names, stage_hp_list)):
        # run only specific stage (defined by cli arg)
        stage_idx_cliarg = cli_args.stage_idx
        if stage_idx_cliarg is not None and stage_idx_cliarg != i:
            continue

        # generate stage_id and sync across devices
        stage_id = generate_id()
        if is_distributed():
            object_list = [stage_id] if is_rank0() else [None]
            broadcast_object_list(object_list)
            stage_id = object_list[0]

        # train stages
        if stage_name is None:
            stage_name = stage_hp.get("stage_name", "default_stage")
        max_batch_size = max_batch_sizes[stage_name] if stage_name in max_batch_sizes else None
        train_stage(
            stage_hp=stage_hp,
            static_config=static_config,
            cliargs=cli_args,
            device=device,
            stage_name=stage_name,
            stage_id=stage_id,
            max_batch_size=max_batch_size,
            previous_stage_ids=stage_ids,
        )
        # remember stage_id for next stages
        stage_ids[stage_name] = stage_id


def main():
    # parse cli_args immediately for fast cli_args validation
    cli_args = parse_run_cli_args()
    static_config = StaticConfig(uri="static_config.yaml", datasets_were_preloaded=cli_args.datasets_were_preloaded)
    # initialize loggers for setup (seperate id)
    add_global_handlers(log_file_uri=None)

    if is_managed():
        run_managed(
            accelerator=cli_args.accelerator,
            devices=cli_args.devices,
            main_single=main_single,
        )
    else:
        run_single_or_multiprocess(
            accelerator=cli_args.accelerator,
            devices=cli_args.devices,
            main_single=main_single,
            master_port=cli_args.master_port or static_config.master_port,
            mig_devices=static_config.mig_config,
        )


if __name__ == "__main__":
    # check instantly to skip the whole multiprocessing setup in case of wrong versions
    check_versions(verbose=False)
    main()
