#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import json
import os
import numpy as np
import random
import time

from parlai.core.message import Message
from parlai.core.worlds import validate
import parlai.utils.logging as logging
from parlai.crowdsourcing.tasks.model_chat.utils import Compatibility
from parlai.crowdsourcing.tasks.model_chat.worlds import (
    get_bot_worker,
    ModelChatWorld,
    ModelChatOnboardWorld,
)

PERSONA_SETTER_AGENT = 'persona-agent'


class MultipartyChatWorld(ModelChatWorld):
    def __init__(self, opt, agent, bot, context_info=None):
        super(ModelChatWorld, self).__init__(opt, agent=agent, bot=bot)
        self.context_info = context_info
        self.personas = self.context_info['personas']

    def _run_initial_turn(self) -> None:
        """
        Run the initial turn for both the human and the bot.

        Optionally show the bot its persona. If we are in BST conversation mode, show 2
        previous BST utterances to both the human and the bot; if we are in Meena-like
        conversation mode, show "Hi!" to the human and the bot and let the bot respond
        accordingly.
        """
        # Removing the agents ids for use the id names for the agents.
        self.bot.agent_id = None
        self.agent.agent_id = None

        if self.opt['include_persona']:
            # Sending persona to the bot agent
            message = {"episode_done": False}
            assert isinstance(
                self.personas[1], dict
            ), 'Unknown persona format. Check the ContextGenerator in your task.'
            message['id'] = PERSONA_SETTER_AGENT
            message['text'] = 'PERSONA SETTING MESSAGE'
            message['personas'] = self.personas
            if 'location' in self.context_info:
                message['location'] = self.context_info['location']
            # The bot seeing its persona does not count as a "turn"
            self.bot.observe(validate(message), increment_turn=False)

            # Sending persona to the agent
            self.agent.observe(
                validate(
                    {
                        'episode_done': False,
                        'id': PERSONA_SETTER_AGENT,
                        'task_data': {
                            'personas': self.personas,
                            'location': self.context_info['location'],
                        },
                    }
                )
            )

    def has_final_rating(self, act):
        return act.get('task_data', {}).get('final_rating') is not None

    def parley(self):
        act = None  # Adding this for linter errors.
        logging.verbose(
            f'{self.__class__.__name__}:{self.tag}: is at turn {self.task_turn_idx}, with {self.num_turns} pairs of turns needed...'
        )

        if self.task_turn_idx == 0:
            self._run_initial_turn()
            self.task_turn_idx += 1
            return

        """Otherwise, we proceed accordingly"""
        logging.verbose(
            f'{self.__class__.__name__}:{self.tag}: About to act with task turn idx: {self.task_turn_idx}'
        )

        if not self.chat_done:
            act = self.bot.act()
            human_turn = act.get('human_turn', False)
            # Bot decided it is human turn.

            if human_turn:
                act = self.agent.act(timeout=self.max_resp_time)
                self.chat_done = self.has_final_rating(act)
                Compatibility.backward_compatible_force_set(
                    act, 'id', self.personas[0]['name']
                )

                act = Message(Compatibility.maybe_fix_act(act)).json_safe_payload()
            utterance_data = {
                'agent_idx': 0 if human_turn else 1,
                # Get rid of annotations HTML if it's the bot response
                'text': act['text'].split('<br>')[0],
                'id': act.get('id', 'NULL_ID'),
            }
            self.dialog.append(utterance_data)

            if human_turn:
                self.bot.observe(validate(act))
            else:
                act['needs_rating'] = True
                self.agent.observe(validate(act))

                # The new act replaces the old one
                act = self.agent.act(timeout=self.max_resp_time)
                act.force_set('text', 'THIS IS A RATING ACTION')
                p = act['task_data'].get('problem_data_for_prior_message')
                if p is not None:
                    turn_idx = -1
                    # Attach the problem data to the last utterance (just generated by bot).
                    self.__add_problem_data_to_utterance(p, turn_idx=turn_idx)

                self.chat_done = self.has_final_rating(act)

            self.task_turn_idx += 1

        if self.chat_done:
            self.dialog[-1]['final_rating'] = act['task_data']['final_rating']

            # Save the final chat data
            date_folder = time.strftime('%Y_%m_%d')
            time_string = time.strftime('%Y%m%d_%H%M%S')
            chat_data_subfolder = os.path.join(
                self.opt['chat_data_folder'], date_folder
            )
            os.makedirs(chat_data_subfolder, exist_ok=True)
            chat_data_path = os.path.join(
                chat_data_subfolder,
                f'{time_string}_{np.random.randint(0, 1000)}_{self.task_type}.json',
            )
            self.final_chat_data = self.get_final_chat_data()
            self.agent.mephisto_agent.state.messages.append(
                {
                    'final_chat_data': self.final_chat_data,
                    'data': {},
                    'packet_type': None,
                    'timestamp': None,
                }
            )
            # Append the chat data directly to the agent state's message list in
            # order to prevent the worker from seeing a new text response in the UI.
            # Add some dummy keys for compatibility with all agent state messages
            # TODO: remove this when no longer saving data to disk manually
            with open(chat_data_path, 'w+') as f_json:
                data_str = json.dumps(self.final_chat_data)
                f_json.write(data_str)
            logging.info(
                f'{self.__class__.__name__}:{self.tag}: Data saved at '
                f'{chat_data_path} for model: {self.bot.worker_id}.'
            )

            # Soft-block the worker if there were acceptability violations
            acceptability_violations = self.final_chat_data['acceptability_violations'][
                0
            ]
            if acceptability_violations is not None and acceptability_violations != '':
                logging.warning(
                    f'**NOTE** Acceptability violations detected: {acceptability_violations}'
                )
                # Grant the failed qualification
                self.agent.mephisto_agent.get_worker().grant_qualification(
                    self.block_qualification, 1
                )

    def __add_problem_data_to_utterance(self, p, turn_idx: int):
        """
        Attach problem data to the bot's prior utterance, given by turn_idx.

        This is copied exactly from the main model_chat world.
        """
        logging.verbose(f'Problem matrix:\n{p}')
        assert (
            self.dialog[turn_idx]['agent_idx'] == 1
        ), 'Problem data must be attached to a bot utterance.'
        assert (
            'problem_data' not in self.dialog[turn_idx]
        ), "Don't overwrite existing problem data!"
        self.dialog[turn_idx]['problem_data'] = p


class MultiLightModelChatOnboardWorld(ModelChatOnboardWorld):
    pass


def make_onboarding_world(opt, agent):
    return MultiLightModelChatOnboardWorld(opt, agent)


def make_world(opt, agents):

    # Extract important components from opt
    statistics_condition = opt['statistics_condition']
    context_generator = opt['context_generator']

    # Get context: personas, previous utterances, etc.
    if context_generator is not None:
        context_info = context_generator.get_context()
    else:
        context_info = None

    # Decide on a bot to use
    run_statistics = opt['run_statistics']
    with statistics_condition:
        remaining_counts_needed = [
            (m, c - run_statistics[m]) for (m, c) in opt['conversations_needed'].items()
        ]
        remaining_counts_needed.sort(reverse=True, key=lambda x: x[1])
        model_name = remaining_counts_needed[0][0]
        print(f'Remaining conversation counts needed: {remaining_counts_needed}')
        print(f'Choosing the "{model_name}" model for the bot.')
    bot_worker = get_bot_worker(opt=opt, model_name=model_name)

    return MultipartyChatWorld(
        opt, agent=agents[0], bot=bot_worker, context_info=context_info
    )


def get_world_params():
    return {"agent_count": 1}


def get_settings(opt=None):
    """
    Returns the conversation settings.

    This is a place-holder function with a few hand selected settings, override with
    more for real data collection.
    """
    return [
        {
            'personas': [
                {
                    'name': 'grass snake',
                    'persona': "I'm a grass snake. I slither around the castle and fields. I eat the rodents that eat the grain.",
                },
                {
                    'name': 'tribesman',
                    'persona': (
                        "I am a tribesman in my group. I am known as a leader in my community and love to help my people. "
                        " I'm very level headed and don't get angry easily. Many of my peers come to me to solve disagreements."
                    ),
                },
                {
                    'name': 'thief',
                    'persona': (
                        'I live alone in a tent in the woods. I steal food from the townspeople and coal from the blacksmith.'
                        ' The village police can not find me to put me in jail.'
                    ),
                },
            ],
            'location': {
                'name': 'Bamboo hut',
                'description': (
                    "Built of bamboo trunks and a bamboo leaf roof, this small hut has one window on each side and a short door,"
                    " where those who enter must stoop down so they don't hit their heads. "
                    "A dirt floor is covered with palm fronds gathered from the jungle; "
                    "four small rocks are placed around the center of the room, forming a place for the occupants to sit. "
                    "A small fire burns just outside of the hut, and a wooden spit is suspended over the fire. "
                    "One of the support poles of the hut has a woven grass bag hanging from it. The bag contains a half-dozen coconuts,"
                    " clearly gathered for consuming at a later time. A colorful lizard is sleeping in the sun in one of the windows."
                ),
            },
        },
        {
            'personas': [
                {
                    'name': 'clergy',
                    'persona': (
                        "I oversee the castle's chapel.  I collect alms for the poor. "
                        "I am the spiritual leader of the subjects of the kingdom."
                    ),
                },
                {
                    'name': 'Nuns',
                    'persona': (
                        "I am a nun and I live in a monastery with others nuns and fathers who server the king."
                        " I pray to the lord everyday that Queen remains in good health. "
                        "I was made a sister at a young age and didn't have a choice. "
                        "I will never know what being with a man will feel like."
                    ),
                },
                {
                    'name': 'priest',
                    'persona': 'I am here to help the needy. I am well respected in the town. I can not accept lying.',
                },
            ],
            'location': {
                'name': 'Church Entryway',
                'description': (
                    'The church has marble floors and a huge frost window. '
                    'There are benches made from wood and a big organ can be seen at the front stage.'
                    ' There is gold trim all around the church.'
                ),
            },
        },
    ]


class ContextGenerator:
    """
    Generates contexts shown to crowdsourced workers during data collection.
    """

    def __init__(self, opt, datatype: str = 'test', seed: int = None):
        """
        Initalize the context generator.
        """
        if seed is not None:
            self.rng = random.Random(seed)
        else:
            self.rng = random.Random()

    def get_context(self) -> dict:
        """
        Get context information to be shown at the beginning of one conversation. Values
        in return dict:

        - context_dataset: the dataset
        - personas: a list of dict where each dictionary is a persona as stored in this task messages.
        """
        setting = random.choice(get_settings())
        return {
            'context_dataset': 'multi-modelchat',
            'personas': setting['personas'],
            'location': setting['location'],
        }
