# Copyright (c) 2021, salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root
# or https://opensource.org/licenses/BSD-3-Clause
#
"""
The Fully Connected Network class
"""

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from gym.spaces import Box, Dict, Discrete, MultiDiscrete

from warp_drive.utils.constants import Constants
from warp_drive.utils.data_feed import DataFeed

_OBSERVATIONS = Constants.OBSERVATIONS
_ACTION_MASK = Constants.ACTION_MASK


def apply_logit_mask(logits, mask=None):
    """
    Mask values of 1 are valid actions.
    Add huge negative values to logits with 0 mask values.
    """
    if mask is None:
        return logits
    
    logit_mask = torch.ones_like(logits) * -10000000
    logit_mask = logit_mask * (1 - mask)
    return logits + logit_mask


# Policy networks
# ---------------
class FullyConnected(nn.Module):
    """
    Fully connected network implementation in Pytorch
    """

    name = "torch_fully_connected"

    def __init__(
        self,
        env,
        model_config,
        policy,
        policy_tag_to_agent_id_map,
        create_separate_placeholders_for_each_policy=False,
        obs_dim_corresponding_to_num_agents="first",
    ):
        super().__init__()

        self.env = env
        fc_dims = model_config["fc_dims"]
        assert isinstance(fc_dims, list)
        num_fc_layers = len(fc_dims)
        self.policy = policy
        self.policy_tag_to_agent_id_map = policy_tag_to_agent_id_map
        self.create_separate_placeholders_for_each_policy = (
            create_separate_placeholders_for_each_policy
        )
        assert obs_dim_corresponding_to_num_agents in ["first", "last"]
        self.obs_dim_corresponding_to_num_agents = obs_dim_corresponding_to_num_agents
        self.fast_forward_mode = False

        sample_agent_id = self.policy_tag_to_agent_id_map[self.policy][0]
        # Flatten obs space
        self.observation_space = self.env.env.observation_space[sample_agent_id]
        flattened_obs_size = self.get_flattened_obs_size()

        if isinstance(self.env.env.action_space[sample_agent_id], Discrete):
            action_space = [self.env.env.action_space[sample_agent_id].n]
        elif isinstance(self.env.env.action_space[sample_agent_id], MultiDiscrete):
            action_space = self.env.env.action_space[sample_agent_id].nvec
        else:
            raise NotImplementedError

        input_dims = [flattened_obs_size] + fc_dims[:-1]
        output_dims = fc_dims

        self.fc = nn.ModuleDict()
        for fc_layer in range(num_fc_layers):
            self.fc[str(fc_layer)] = nn.Sequential(
                nn.Linear(input_dims[fc_layer], output_dims[fc_layer]), nn.ReLU(),
            )

        # policy network (list of heads)
        policy_heads = [None for _ in range(len(action_space))]
        for idx, act_space in enumerate(action_space):
            policy_heads[idx] = nn.Linear(fc_dims[-1], act_space)
        self.policy_head = nn.ModuleList(policy_heads)

        # value-function network head
        self.vf_head = nn.Linear(fc_dims[-1], 1)

        # used for action masking
        self.action_mask = None

    def get_flattened_obs_size(self):
        if isinstance(self.observation_space, Box):
            obs_size = np.prod(self.observation_space.shape)
        elif isinstance(self.observation_space, Dict):
            obs_size = 0
            for key in self.observation_space:
                if key == _ACTION_MASK:
                    pass
                else:
                    obs_size += np.prod(self.observation_space[key].shape)
        else:
            raise NotImplementedError("Observation space must be of Box or Dict type")
        return int(obs_size)

    def reshape_and_flatten_obs(self, obs):
        """
        # Note: WarpDrive assumes that all the observation are shaped
        # (num_agents, *feature_dim), i.e., the observation dimension
        # corresponding to 'num_agents' is the first one. If the observation
        # dimension corresponding to num_agents is last, we will need to
        # permute the axes to align with WarpDrive's assumption.
        """
        num_envs = obs.shape[0]
        if self.create_separate_placeholders_for_each_policy:
            num_agents = len(self.policy_tag_to_agent_id_map[self.policy])
        else:
            num_agents = self.env.n_agents

        if self.obs_dim_corresponding_to_num_agents == "first":
            pass
        elif self.obs_dim_corresponding_to_num_agents == "last":
            shape_len = len(obs.shape)
            if shape_len == 1:
                obs = obs.reshape(-1, num_agents)  # valid only when num_agents = 1
            obs = obs.permute(0, -1, *[dim for dim in range(1, shape_len - 1)])
        else:
            raise ValueError(
                "num_agents can only be the first or last dimension in the observations."
            )
        return obs.reshape(num_envs, num_agents, -1)

    def get_flattened_obs(self):
        if isinstance(self.observation_space, Box):
            if self.create_separate_placeholders_for_each_policy:
                obs = self.env.cuda_data_manager.data_on_device_via_torch(
                    f"{_OBSERVATIONS}_{self.policy}"
                )
            else:
                obs = self.env.cuda_data_manager.data_on_device_via_torch(_OBSERVATIONS)

            flattened_obs = self.reshape_and_flatten_obs(obs)
        elif isinstance(self.observation_space, Dict):
            obs_dict = {}
            for key in self.observation_space:
                if self.create_separate_placeholders_for_each_policy:
                    obs = self.env.cuda_data_manager.data_on_device_via_torch(
                        f"{_OBSERVATIONS}_{self.policy}_{key}"
                    )
                else:
                    obs = self.env.cuda_data_manager.data_on_device_via_torch(
                        f"{_OBSERVATIONS}_{key}"
                    )

                if key == _ACTION_MASK:
                    self.action_mask = self.reshape_and_flatten_obs(obs)
                else:
                    obs_dict[key] = obs

            flattened_obs_dict = {}
            for key in obs_dict:
                flattened_obs_dict[key] = self.reshape_and_flatten_obs(obs_dict[key])
            flattened_obs = torch.cat(list(flattened_obs_dict.values()), dim=-1)
        else:
            raise NotImplementedError("Observation space must be of Box or Dict type")
        return flattened_obs

    def set_fast_forward_mode(self):
        # if there is only one policy with discrete action space,
        # then there is no need to map to agents
        self.fast_forward_mode = True
        print(
            f"the model {self.name} turns on the fast_forward_mode to speed up "
            "the forward calculation (there is only one policy with discrete "
            "action space, therefore in the model forward there is no need to have "
            "an explicit mapping to agents which is slow) "
        )

    def forward(self, obs=None):
        if obs is None:
            obs = self.get_flattened_obs()

            if (
                not self.fast_forward_mode
                and not self.create_separate_placeholders_for_each_policy
            ):
                agent_ids_for_policy = self.policy_tag_to_agent_id_map[self.policy]
                ip = obs[:, agent_ids_for_policy]
            else:
                ip = obs
        else:
            ip = obs

        # Feed through the FC layers
        for layer in range(len(self.fc)):
            op = self.fc[str(layer)](ip)
            ip = op

        # Compute the action probabilities and the value function estimate
        # Apply action mask to the logits as well.
        action_probs = [
            F.softmax(apply_logit_mask(ph(op), self.action_mask), dim=-1)
            for ph in self.policy_head
        ]
        vals = self.vf_head(op)

        return action_probs, vals[..., 0]
