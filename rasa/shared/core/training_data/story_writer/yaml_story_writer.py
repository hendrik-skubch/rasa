from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Text, Union, Optional

from ruamel import yaml
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString, LiteralScalarString

import rasa.shared.utils.io
import rasa.shared.core.constants
from rasa.shared.constants import LATEST_TRAINING_DATA_FORMAT_VERSION
from rasa.shared.core.events import (
    UserUttered,
    ActionExecuted,
    SlotSet,
    ActiveLoop,
    Event,
)
from rasa.shared.core.training_data.story_reader.yaml_story_reader import (
    KEY_STORIES,
    KEY_STORY_NAME,
    KEY_USER_INTENT,
    KEY_ENTITIES,
    KEY_ACTION,
    KEY_STEPS,
    KEY_CHECKPOINT,
    KEY_SLOT_NAME,
    KEY_CHECKPOINT_SLOTS,
    KEY_OR,
    KEY_USER_MESSAGE,
    KEY_ACTIVE_LOOP,
    KEY_RULES,
    KEY_RULE_FOR_CONVERSATION_START,
    KEY_WAIT_FOR_USER_INPUT_AFTER_RULE,
    KEY_RULE_CONDITION,
    KEY_RULE_NAME,
)
from rasa.shared.core.training_data.structures import (
    StoryStep,
    Checkpoint,
    STORY_START,
    RuleStep,
)


class YAMLStoryWriter:
    """Writes Core training data into a file in a YAML format. """

    def dumps(self, story_steps: List[StoryStep]) -> Text:
        """Turns Story steps into a string.

        Args:
            story_steps: Original story steps to be converted to the YAML.
        Returns:
            String with story steps in the YAML format.
        """
        stream = yaml.StringIO()
        self.dump(stream, story_steps)
        return stream.getvalue()

    def dump(
        self, target: Union[Text, Path, yaml.StringIO], story_steps: List[StoryStep]
    ) -> None:
        """Writes Story steps into a target file/stream.

        Args:
            target: name of the target file/stream to write the YAML to.
            story_steps: Original story steps to be converted to the YAML.
        """
        result = self.stories_to_yaml(story_steps)

        rasa.shared.utils.io.write_yaml(result, target, True)

    def stories_to_yaml(self, story_steps: List[StoryStep]) -> Dict[Text, Any]:
        """Converts a sequence of story steps into yaml format.

        Args:
            story_steps: Original story steps to be converted to the YAML.
        """
        from rasa.shared.utils.validation import KEY_TRAINING_DATA_FORMAT_VERSION

        stories = []
        rules = []
        for story_step in story_steps:
            if isinstance(story_step, RuleStep):
                rules.append(self.process_rule_step(story_step))
            else:
                stories.append(self.process_story_step(story_step))

        result = OrderedDict()
        result[KEY_TRAINING_DATA_FORMAT_VERSION] = DoubleQuotedScalarString(
            LATEST_TRAINING_DATA_FORMAT_VERSION
        )

        if stories:
            result[KEY_STORIES] = stories
        if rules:
            result[KEY_RULES] = rules
        return result

    def process_story_step(self, story_step: StoryStep) -> OrderedDict:
        """Converts a single story step into an ordered dict.

        Args:
            story_step: A single story step to be converted to the dict.

        Returns:
            Dict with a story step.
        """
        result = OrderedDict()
        result[KEY_STORY_NAME] = story_step.block_name
        steps = self.process_checkpoints(story_step.start_checkpoints)

        for event in story_step.events:
            processed = self.process_event(event)
            if processed:
                steps.append(processed)

        steps.extend(self.process_checkpoints(story_step.end_checkpoints))

        result[KEY_STEPS] = steps

        return result

    def process_event(self, event: Event) -> Optional[OrderedDict]:
        if isinstance(event, list):
            return self.process_or_utterances(event)
        if isinstance(event, UserUttered):
            return self.process_user_utterance(event)
        if isinstance(event, ActionExecuted):
            return self.process_action(event)
        if isinstance(event, SlotSet):
            return self.process_slot(event)
        if isinstance(event, ActiveLoop):
            return self.process_active_loop(event)
        return None

    @staticmethod
    def stories_contain_loops(stories: List[StoryStep]) -> bool:
        """Checks if the stories contain at least one active loop.

        Args:
            stories: Stories steps.

        Returns:
            `True` if the `stories` contain at least one active loop.
            `False` otherwise.
        """
        return any(
            [
                [event for event in story_step.events if isinstance(event, ActiveLoop)]
                for story_step in stories
            ]
        )

    @staticmethod
    def _text_is_real_message(user_utterance: UserUttered) -> bool:
        return (
            not user_utterance.intent
            or user_utterance.text != user_utterance.as_story_string()
        )

    @staticmethod
    def process_user_utterance(user_utterance: UserUttered) -> OrderedDict:
        """Converts a single user utterance into an ordered dict.

        Args:
            user_utterance: Original user utterance object.

        Returns:
            Dict with a user utterance.
        """
        result = CommentedMap()
        result[KEY_USER_INTENT] = user_utterance.intent["name"]

        if hasattr(user_utterance, "inline_comment"):
            result.yaml_add_eol_comment(
                user_utterance.inline_comment(), KEY_USER_INTENT
            )

        if (
            YAMLStoryWriter._text_is_real_message(user_utterance)
            and user_utterance.text
        ):
            result[KEY_USER_MESSAGE] = LiteralScalarString(user_utterance.text)

        if len(user_utterance.entities):
            entities = []
            for entity in user_utterance.entities:
                if entity["value"]:
                    entities.append(OrderedDict([(entity["entity"], entity["value"])]))
                else:
                    entities.append(entity["entity"])
            result[KEY_ENTITIES] = entities

        return result

    @staticmethod
    def process_action(action: ActionExecuted) -> Optional[OrderedDict]:
        """Converts a single action into an ordered dict.

        Args:
            action: Original action object.

        Returns:
            Dict with an action.
        """
        if action.action_name == rasa.shared.core.constants.RULE_SNIPPET_ACTION_NAME:
            return None

        result = CommentedMap()
        result[KEY_ACTION] = action.action_name

        if hasattr(action, "inline_comment"):
            result.yaml_add_eol_comment(action.inline_comment(), KEY_ACTION)

        return result

    @staticmethod
    def process_slot(event: SlotSet):
        """Converts a single `SlotSet` event into an ordered dict.

        Args:
            event: Original `SlotSet` event.

        Returns:
            Dict with an `SlotSet` event.
        """
        return OrderedDict([(KEY_SLOT_NAME, [{event.key: event.value}])])

    @staticmethod
    def process_checkpoints(checkpoints: List[Checkpoint]) -> List[OrderedDict]:
        """Converts checkpoints event into an ordered dict.

        Args:
            checkpoints: List of original checkpoint.

        Returns:
            List of converted checkpoints.
        """
        result = []
        for checkpoint in checkpoints:
            if checkpoint.name == STORY_START:
                continue
            next_checkpoint = OrderedDict([(KEY_CHECKPOINT, checkpoint.name)])
            if checkpoint.conditions:
                next_checkpoint[KEY_CHECKPOINT_SLOTS] = [
                    {key: value} for key, value in checkpoint.conditions.items()
                ]
            result.append(next_checkpoint)
        return result

    def process_or_utterances(self, utterances: List[UserUttered]) -> OrderedDict:
        """Converts user utterance containing the `OR` statement.

        Args:
            utterances: User utterances belonging to the same `OR` statement.

        Returns:
            Dict with converted user utterances.
        """
        return OrderedDict(
            [
                (
                    KEY_OR,
                    [
                        self.process_user_utterance(utterance)
                        for utterance in utterances
                    ],
                )
            ]
        )

    @staticmethod
    def process_active_loop(event: ActiveLoop) -> OrderedDict:
        """Converts ActiveLoop event into an ordered dict.

        Args:
            event: ActiveLoop event.

        Returns:
            Converted event.
        """
        return OrderedDict([(KEY_ACTIVE_LOOP, event.name)])

    def process_rule_step(self, rule_step: RuleStep) -> OrderedDict:
        """Converts a RuleStep into an ordered dict.

        Args:
            rule_step: RuleStep object.

        Returns:
            Converted rule step.
        """
        result = OrderedDict()
        result[KEY_RULE_NAME] = rule_step.block_name

        condition_steps = []
        condition_events = rule_step.get_rules_condition()
        for event in condition_events:
            processed = self.process_event(event)
            if processed:
                condition_steps.append(processed)
        if condition_steps:
            result[KEY_RULE_CONDITION] = condition_steps

        normal_events = rule_step.get_rules_events()
        if normal_events and not (
            isinstance(normal_events[0], ActionExecuted)
            and normal_events[0].action_name
            == rasa.shared.core.constants.RULE_SNIPPET_ACTION_NAME
        ):
            result[KEY_RULE_FOR_CONVERSATION_START] = True

        normal_steps = []
        for event in normal_events:
            processed = self.process_event(event)
            if processed:
                normal_steps.append(processed)
        if normal_steps:
            result[KEY_STEPS] = normal_steps

        if len(normal_events) > 1 and (
            isinstance(normal_events[len(normal_events) - 1], ActionExecuted)
            and normal_events[len(normal_events) - 1].action_name
            == rasa.shared.core.constants.RULE_SNIPPET_ACTION_NAME
        ):
            result[KEY_WAIT_FOR_USER_INPUT_AFTER_RULE] = False

        return result
