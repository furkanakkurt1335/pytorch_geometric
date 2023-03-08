import torch
from torch import Tensor
from torch_geometric.nn.to_hetero_module import ToHeteroLinear
from torch_geometric.nn.dense.linear import HeteroDictLinear
from torch_geometric.typing import Metadata, NodeType, EdgeType
from torch_geometric.nn.parameter_dict import ParameterDict
from typing import Dict, Union, Optional, List

accepted_norm_types = ["batchnorm", "instancenorm", "layernorm"]

class _HeteroNorm(torch.nn.Module):
    r"""Applies normalization over node features for each node type using:
    BatchNorm <https://pytorch-geometric.readthedocs.io/en/latest/generated/torch_geometric.nn.norm.BatchNorm.html#torch_geometric.nn.norm.BatchNorm>,
    InstanceNorm <https://pytorch-geometric.readthedocs.io/en/latest/generated/torch_geometric.nn.norm.InstanceNorm.html#torch_geometric.nn.norm.InstanceNorm>,
    or LayerNorm <https://pytorch-geometric.readthedocs.io/en/latest/generated/torch_geometric.nn.norm.LayerNorm.html#torch_geometric.nn.norm.LayerNorm>

    Args:
        in_channels (int): Size of each input sample.
            Use :obj:`-1` for lazy initialization.
        norm_type (str): Which of "BatchNorm", "InstanceNorm", "LayerNorm" to use
            (default: "BatchNorm")
        types (List[str], optional): Only needed if in_channels
            is passed as an int.
        eps (float, optional): A value added to the denominator for numerical
            stability. (default: :obj:`1e-5`)
        momentum (float, optional): The value used for the running mean and
            running variance computation. (default: :obj:`0.1`)
        affine (bool, optional): If set to :obj:`True`, this module has
            learnable affine parameters :math:`\gamma` and :math:`\beta`.
            (default: :obj:`True`)
        track_running_stats (bool, optional): If set to :obj:`True`, this
            module tracks the running mean and variance, and when set to
            :obj:`False`, this module does not track such statistics and always
            uses batch statistics in both training and eval modes.
            (default: :obj:`True`)
        allow_single_element (bool, optional): If set to :obj:`True`, batches
            with only a single element will work as during in evaluation.
            That is the running mean and variance will be used.
            Requires :obj:`track_running_stats=True`. (default: :obj:`False`)
    """
    def __init__(self, in_channels: int, norm_type: str,
                 types: Union[List[NodeType],List[EdgeType]],
                 eps: float = 1e-5,
                 momentum: float = 0.1, affine: bool = True,
                 track_running_stats: bool = True,
                 allow_single_element: bool = False):
        super().__init__()
        if not norm_type.lower() in accepted_norm_types:
            raise ValueError('Please choose norm type from "BatchNorm", "InstanceNorm", "LayerNorm"')
        self.norm_type = norm_type.lower()
        if allow_single_element and not track_running_stats:
            raise ValueError("'allow_single_element' requires "
                             "'track_running_stats' to be set to `True`")
        if in_channels == -1:
            self._hook = self.register_forward_pre_hook(
                self.initialize_parameters)
        self.types = list(types)
        if self.types is None:
            raise ValueError("Please provide a list of types if \
                passing `in_channels` as an int")
        self.eps = eps
        self.momentum = momentum
        self.track_running_stats = track_running_stats
        self.allow_single_element = allow_single_element
        self.affine = affine
        self.in_channels = in_channels
        if self.affine:
            self.hetero_linear = HeteroDictLinear(self.in_channels, self.in_channels, self.types)
        if not hasattr(self, "_hook") and self.track_running_stats:
            self.running_means = ParameterDict({mean_type:torch.zeros(self.in_channels) for mean_typ in self.types})
            self.running_vars = ParameterDict({var_type:torch.ones(self.in_channels) for var_type in self.types})
        self.allow_single_element = allow_single_element
        self.reset_parameters()

    @torch.no_grad()
    def initialize_parameters(self, module, input):
        self.in_channels = {}
        if self.affine:
            self.hetero_linear.initialize_parameters(None, input)
        xs = list(input[0].values())
        self.in_channels = xs[0].size(-1)
        assert all([x_n.size(-1)==self.in_channels for x_n in xs]), "All inputs must have same num features"
        self.reset_parameters()
        self._hook.remove()
        delattr(self, '_hook')

    @classmethod
    def from_homogeneous(cls, norm_module: torch.nn.Module, types: Union[List[NodeType],List[EdgeType]]):
        norm_type = None
        for norm_type_i in accepted_norm_types:
            if norm_type_i in str(norm_module).lower(): 
                norm_type = norm_type_i 
        if norm_type is None:
            raise ValueError('Please only pass one of "BatchNorm", "InstanceNorm", "LayerNorm"')
        try:
            # pyg norms
            in_channels = norm_module.in_channels
        except AttributeError:
            try:
                # torch native batch/instance norm
                in_channels = norm_module.num_features
            except AttributeError:
                # torch native layer norm
                in_channels = norm_module.normalized_shape
                if not isinstance(in_channels, int):
                    raise ValueError("If making torch.nn.LayerNorm heterogeneous, \
                        please ensure that `normalized_shape` is a single integer")
                if norm_module.mode == "graph":
                    raise ValueError("If making torch.nn.LayerNorm heterogeneous, \
                        please ensure that mode == 'node'")
        in_channels = {node_type: in_channels for node_type in types}
        try:
            eps = norm_module.eps
        except:
            eps = norm_module.module.eps
        try:
            # store batch/instance norm
            momentum = norm_module.momentum
            track_running_stats = norm_module.track_running_stats
        except AttributeError:
            # layer norm
            momentum = None
            track_running_stats = False

        try:
            # PyG norms
            affine = norm_module.module.affine
        except AttributeError:
            try:
                # torch native batch/instance norm
                affine = norm_module.affine
            except AttributeError:
                # torch native layer norm
                affine = norm_module.elementwise_affine
        if hasattr(norm_module, "allow_single_element"):
            allow_single_element = norm_module.allow_single_element
        else:
            allow_single_element = False
        print(cls)
        print(str(cls))
        return cls(in_channels, norm_type, types, eps, momentum, affine, track_running_stats, allow_single_element)


    def reset_parameters(self):
        r"""Resets all learnable parameters of the module."""
        if self.affine:
            self.hetero_linear.reset_parameters()
        if self.track_running_stats:
            for type_i in self.types:
                self.running_means[type_i] = torch.zeros(self.in_channels)
                self.running_vars[type_i] = torch.ones(self.in_channels)

    def fused_forward(self, x: Tensor, type_vec: Tensor) -> Tensor:
        out = x.new_empty(x.size(0), self.in_channels)
        x_dict = {self.types[i]:x[type_vec == i] for i in range(len(self.types))}
        return dict_forward(x_dict)

    def dict_forward(
        self,
        x_dict: Dict[Union[NodeType, EdgeType], Tensor],
    ) -> Dict[Union[NodeType, EdgeType], Tensor]:
        out_dict = {}
        for x_type, x in x_dict.items():
            if self.allow_single_element and x.size(0) <= 1:
                # for inference
                mean_x, var_x = self.running_means[x_type], self.running_vars[x_type]
            else:
                # for training
                mean_x = torch.mean(x, dim=0)
                var_x = torch.var(x, unbiased=False, dim=0)
                if self.track_running_stats:
                    self.running_means[x_type] = self.momentum * self.running_means[x_type] + (1 - self.momentum) * mean_x
                    self.running_vars[x_type] = torch.sqrt(self.momentum * torch.square(self.running_vars[x_type]) + (1 - self.momentum) * torch.square(var_x))
                    mean_x, var_x = self.running_means[x_type], self.running_vars[x_type]
            out_dict[x_type] = (x - mean_x) / torch.sqrt(var_x + self.eps)
        if self.affine:
            return self.hetero_linear(out_dict)
        else:
            return out_dict

    def forward(
        self,
        x: Union[Tensor, Dict[Union[NodeType, EdgeType], Tensor]],
        type_vec: Optional[Tensor] = None,
    ) -> Union[Tensor, Dict[Union[NodeType, EdgeType], Tensor]]:

        if isinstance(x, dict):
            return self.dict_forward(x)

        elif isinstance(x, Tensor) and type_vec is not None:
            return self.fused_forward(x, type_vec)

        raise ValueError(f"Encountered invalid forward types in "
                         f"'{self.__class__.__name__}'")

    def __repr__(self):
        return f'{self.__class__.__name__}({self.module.num_features})'


class HeteroBatchNorm(_HeteroNorm):
    def __init__(self, in_channels: int,
                 types: Union[List[NodeType],List[EdgeType]],
                 eps: float = 1e-5,
                 momentum: float = 0.1, affine: bool = True,
                 track_running_stats: bool = True,
                 allow_single_element: bool = False):
        super().__init__(in_channels, "BatchNorm",
                 types, eps, momentum, affine,
                 track_running_stats, allow_single_element)


class HeteroInstanceNorm(_HeteroNorm):
    def __init__(self, in_channels: int,
                 types: Union[List[NodeType],List[EdgeType]],
                 eps: float = 1e-5,
                 momentum: float = 0.1, affine: bool = True,
                 track_running_stats: bool = False):
        super().__init__(in_channels, "InstanceNorm",
                 types, eps, momentum, affine,
                 track_running_stats, allow_single_element=False)


class HeteroLayerNorm(_HeteroNorm):
    def __init__(self, in_channels: int,
                 types: Union[List[NodeType],List[EdgeType]],
                 eps: float = 1e-5,
                 momentum: float = 0.1, affine: bool = True):
        super().__init__(in_channels, "LayerNorm",
                 types, eps, 0.0, affine,
                 track_running_stats=False, allow_single_element=False)

