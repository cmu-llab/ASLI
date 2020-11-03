"""This file defines the Monte Carlo Tree Search class. Inspired by https://github.com/suragnair/alpha-zero-general/blob/master/MCTS.py.
"""

from __future__ import annotations

import logging
import math
from typing import Dict, List, Set, Tuple

import numpy as np
import torch
from torch.distributions.categorical import Categorical

from dev_misc import NDA, add_argument, g, get_zeros
from dev_misc.utils import pad_for_log

from .action import SoundChangeAction, SoundChangeActionSpace
from .agent import AgentInputs, AgentOutputs, BasePG
from .env import SoundChangeEnv
from .trajectory import VocabState

# from torch.distributions.distribution import Distribution


class Mcts:

    """Monte Carlo Tree Search class. Everything should be done on cpu except for evaluation.
    Use numpy arrays by default since we can potentially speed up some process through cython
    and parallel processing.
    """

    add_argument('puct_c', default=5.0, dtype=float, msg='Exploration constant.')

    def __init__(self,
                 action_space: SoundChangeActionSpace,
                 agent: BasePG,
                 env: SoundChangeEnv,
                 end_state: VocabState):
        self.action_space = action_space
        self.agent = agent
        self.env = env
        self.end_state = end_state
        self.reset()

    def reset(self):
        self.Psa: Dict[VocabState, NDA] = dict()  # Initial policy.
        self.Wsa: Dict[VocabState, NDA] = dict()
        self.Nsa: Dict[VocabState, NDA] = dict()  # State-action visit counts.

    @torch.no_grad()
    @profile
    def select(self, root: VocabState, depth_limit: int) -> Tuple[VocabState, List[Tuple[VocabState, SoundChangeAction]]]:
        """Select the node to expand."""
        state = root
        last_state = action = None
        path = list()
        while state != self.end_state and state in self.Psa:
            p = self.Psa[state]
            n_s_a = self.Nsa[state]
            n_s = n_s_a.sum()
            w = self.Wsa[state]
            q = w / (n_s_a + 1e-8)
            u = g.puct_c * p * (math.sqrt(n_s) / (1 + n_s_a))

            action_masks = self.action_space.get_permissible_actions(state, return_type='numpy')
            scores = np.where(action_masks, q + u, np.full_like(q, -9999.9, dtype='float32'))
            best_a = scores.argmax(axis=-1)

            action = self.action_space.get_action(best_a)
            path.append((state, action))
            new_state, done, reward = self.env(state, action)

            last_state, state = state, new_state
            depth_limit -= 1
            if depth_limit <= 0:
                break

        # if self.on:
        #     if last_state is None:
        #         logging.debug(f'Selected the root (id {state.s_id}):')
        #         logging.debug(pad_for_log(state.s_key))
        #     else:
        #         logging.debug(f'Path {[state.s_id for state, _ in path]}')
        #         logging.debug(f'Selected the following node (id {state.s_id} from {last_state.s_id}):')
        #         to_log = str(action) + '\n\n'
        #         paddings = max(map(len, last_state.s_key.split('\n'))) + 8
        #         for w0, w1 in zip(last_state.s_key.split('\n'), state.s_key.split('\n')):
        #             to_log += w0 + ' ' * (paddings - len(w0)) + w1 + '\n'
        #         logging.debug(pad_for_log(to_log.strip()))
        #     import time; time.sleep(0.01)

        return state, path

    @torch.no_grad()
    @profile
    def expand(self, state: VocabState) -> float:
        """Expand and evaluate the leaf node."""
        if state == self.end_state:
            return 1.0
        action_masks = self.action_space.get_permissible_actions(state, return_type='tensor')
        policy = self.agent.get_policy(state, action_masks)
        value = self.agent.get_values(state)
        self.Psa[state] = policy.probs.cpu().numpy()
        self.Wsa[state] = np.zeros([len(self.action_space)])
        self.Nsa[state] = np.zeros([len(self.action_space)])

        return value.item()

    @torch.no_grad()
    @profile
    def backup(self, path: List[Tuple[VocabState, SoundChangeAction]], value: float):
        for s, a in path:
            self.Nsa[s][a.action_id] += 1
            self.Wsa[s][a.action_id] += value

    @torch.no_grad()
    @profile
    def play(self, state: VocabState) -> Tuple[NDA, VocabState]:
        exp = np.power(self.Nsa[state], 1.0)
        probs = exp / (exp.sum(axis=-1, keepdims=True) + 1e-8)
        action_id = np.random.choice(range(len(probs)), p=probs)
        action = self.action_space.get_action(action_id)
        new_state, done, reward = self.env(state, action)
        return probs, new_state
