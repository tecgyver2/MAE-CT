import torch


def get_paramnames_with_no_gradient(model):
    return [name for name, param in model.named_parameters() if param.grad is None and param.requires_grad]


def get_output_shape_of_model(model, forward_fn, **forward_kwargs):
    was_in_training_mode = model.training
    # change to eval to not change batchnorm layers
    model.eval()
    # get outputshape from forward pass
    x = torch.ones(1, *model.input_shape, device=model.device)
    output = forward_fn(x, **forward_kwargs)
    if was_in_training_mode:
        model.train()
    return tuple(output.shape[1:])


@torch.no_grad()
def copy_params(source_model, target_model):
    source_parameters = dict({n: v for n, v in source_model.named_parameters()})
    target_parameters = dict({n: v for n, v in target_model.named_parameters()})
    for source_param_name, source_param in source_parameters.items():
        if source_param_name in target_parameters:
            target_param = target_parameters[source_param_name]
            target_param.data = source_param.data


@torch.no_grad()
def update_ema(source_model, target_model, target_factor):
    # TODO i think inplace operations are okay here
    source_parameters = dict({n: v for n, v in source_model.named_parameters()})
    target_parameters = dict({n: v for n, v in target_model.named_parameters()})
    for source_param_name, source_param in source_parameters.items():
        if source_param_name in target_parameters:
            target_param = target_parameters[source_param_name]
            target_param.data = target_param.data * target_factor + source_param.data * (1. - target_factor)

    source_buffers = dict({n: v for n, v in source_model.named_buffers()})
    target_buffers = dict({n: v for n, v in target_model.named_buffers()})
    for source_buffer_name, source_buffer in source_buffers.items():
        if source_buffer_name in target_buffers:
            target_buffer = target_buffers[source_buffer_name]
            target_buffer.data.copy_(source_buffer.data)


def get_named_models(model):
    submodels = model.submodels
    if len(submodels) == 1:
        # single model
        return submodels
    else:
        # composite model
        result = {}
        for name, sub_model in submodels.items():
            named_submodels = get_named_models(sub_model)
            for key, value in named_submodels.items():
                result[f"{name}.{key}"] = value
        return result


def get_trainable_param_count(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def get_frozen_param_count(model):
    return sum(p.numel() for p in model.parameters() if not p.requires_grad)
