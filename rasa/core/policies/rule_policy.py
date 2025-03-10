import logging
from typing import List, Dict, Text, Optional, Any, Set, TYPE_CHECKING, Union

from tqdm import tqdm
import numpy as np
import json

from rasa.shared.constants import DOCS_URL_RULES
from rasa.shared.exceptions import RasaException
import rasa.shared.utils.io
from rasa.shared.core.events import LoopInterrupted, UserUttered, ActionExecuted
from rasa.core.featurizers.tracker_featurizers import TrackerFeaturizer
from rasa.shared.nlu.interpreter import NaturalLanguageInterpreter
from rasa.core.policies.memoization import MemoizationPolicy
from rasa.core.policies.policy import SupportedData
from rasa.shared.core.trackers import (
    DialogueStateTracker,
    get_active_loop_name,
    is_prev_action_listen_in_state,
)
from rasa.shared.core.generator import TrackerWithCachedStates
from rasa.core.constants import DEFAULT_CORE_FALLBACK_THRESHOLD, RULE_POLICY_PRIORITY
from rasa.shared.core.constants import (
    USER_INTENT_RESTART,
    USER_INTENT_BACK,
    USER_INTENT_SESSION_START,
    ACTION_LISTEN_NAME,
    ACTION_RESTART_NAME,
    ACTION_SESSION_START_NAME,
    ACTION_DEFAULT_FALLBACK_NAME,
    ACTION_BACK_NAME,
    RULE_SNIPPET_ACTION_NAME,
    SHOULD_NOT_BE_SET,
    PREVIOUS_ACTION,
    LOOP_REJECTED,
)
from rasa.shared.core.domain import InvalidDomain, State, Domain
from rasa.shared.nlu.constants import ACTION_NAME, INTENT_NAME_KEY
import rasa.core.test


if TYPE_CHECKING:
    from rasa.core.policies.ensemble import PolicyEnsemble  # pytype: disable=pyi-error

logger = logging.getLogger(__name__)

# These are Rasa Open Source default actions and overrule everything at any time.
DEFAULT_ACTION_MAPPINGS = {
    USER_INTENT_RESTART: ACTION_RESTART_NAME,
    USER_INTENT_BACK: ACTION_BACK_NAME,
    USER_INTENT_SESSION_START: ACTION_SESSION_START_NAME,
}

RULES = "rules"
RULES_FOR_LOOP_UNHAPPY_PATH = "rules_for_loop_unhappy_path"
DO_NOT_VALIDATE_LOOP = "do_not_validate_loop"
DO_NOT_PREDICT_LOOP_ACTION = "do_not_predict_loop_action"


class InvalidRule(RasaException):
    """Exception that can be raised when rules are not valid."""

    def __init__(self, message: Text) -> None:
        super().__init__()
        self.message = message

    def __str__(self) -> Text:
        return self.message + (
            f"\nYou can find more information about the usage of "
            f"rules at {DOCS_URL_RULES}. "
        )


class RulePolicy(MemoizationPolicy):
    """Policy which handles all the rules"""

    # rules use explicit json strings
    ENABLE_FEATURE_STRING_COMPRESSION = False

    # number of user inputs that is allowed in case rules are restricted
    ALLOWED_NUMBER_OF_USER_INPUTS = 1

    @staticmethod
    def supported_data() -> SupportedData:
        """The type of data supported by this policy.

        Returns:
            The data type supported by this policy (ML and rule data).
        """
        return SupportedData.ML_AND_RULE_DATA

    def __init__(
        self,
        featurizer: Optional[TrackerFeaturizer] = None,
        priority: int = RULE_POLICY_PRIORITY,
        lookup: Optional[Dict] = None,
        core_fallback_threshold: float = DEFAULT_CORE_FALLBACK_THRESHOLD,
        core_fallback_action_name: Text = ACTION_DEFAULT_FALLBACK_NAME,
        enable_fallback_prediction: bool = True,
        restrict_rules: bool = True,
        check_for_contradictions: bool = True,
    ) -> None:
        """Create a `RulePolicy` object.

        Args:
            featurizer: `Featurizer` which is used to convert conversation states to
                features.
            priority: Priority of the policy which is used if multiple policies predict
                actions with the same confidence.
            lookup: Lookup table which is used to pick matching rules for a conversation
                state.
            core_fallback_threshold: Confidence of the prediction if no rule matched
                and de-facto threshold for a core fallback.
            core_fallback_action_name: Name of the action which should be predicted
                if no rule matched.
            enable_fallback_prediction: If `True` `core_fallback_action_name` is
                predicted in case no rule matched.
        """

        self._core_fallback_threshold = core_fallback_threshold
        self._fallback_action_name = core_fallback_action_name
        self._enable_fallback_prediction = enable_fallback_prediction
        self._restrict_rules = restrict_rules
        self._check_for_contradictions = check_for_contradictions

        # max history is set to `None` in order to capture any lengths of rule stories
        super().__init__(
            featurizer=featurizer, priority=priority, max_history=None, lookup=lookup
        )

    @classmethod
    def validate_against_domain(
        cls, ensemble: Optional["PolicyEnsemble"], domain: Optional[Domain]
    ) -> None:
        if ensemble is None:
            return

        rule_policy = next(
            (p for p in ensemble.policies if isinstance(p, RulePolicy)), None
        )
        if not rule_policy or not rule_policy._enable_fallback_prediction:
            return

        if (
            domain is None
            or rule_policy._fallback_action_name not in domain.action_names
        ):
            raise InvalidDomain(
                f"The fallback action '{rule_policy._fallback_action_name}' which was "
                f"configured for the {RulePolicy.__name__} must be present in the "
                f"domain."
            )

    @staticmethod
    def _is_rule_snippet_state(state: State) -> bool:
        prev_action_name = state.get(PREVIOUS_ACTION, {}).get(ACTION_NAME)
        return prev_action_name == RULE_SNIPPET_ACTION_NAME

    def _create_feature_key(self, states: List[State]) -> Optional[Text]:
        new_states = []
        for state in reversed(states):
            if self._is_rule_snippet_state(state):
                # remove all states before RULE_SNIPPET_ACTION_NAME
                break
            new_states.insert(0, state)

        if not new_states:
            return

        # we sort keys to make sure that the same states
        # represented as dictionaries have the same json strings
        return json.dumps(new_states, sort_keys=True)

    @staticmethod
    def _states_for_unhappy_loop_predictions(states: List[State]) -> List[State]:
        """Modifies the states to create feature keys for loop unhappy path conditions.

        Args:
            states: a representation of a tracker
                as a list of dictionaries containing features

        Returns:
            modified states
        """

        # leave only last 2 dialogue turns to
        # - capture previous meaningful action before action_listen
        # - ignore previous intent
        if len(states) == 1 or not states[-2].get(PREVIOUS_ACTION):
            return [states[-1]]
        else:
            return [{PREVIOUS_ACTION: states[-2][PREVIOUS_ACTION]}, states[-1]]

    @staticmethod
    def _remove_rule_snippet_predictions(lookup: Dict[Text, Text]) -> Dict[Text, Text]:
        # Delete rules if it would predict the RULE_SNIPPET_ACTION_NAME action
        return {
            feature_key: action
            for feature_key, action in lookup.items()
            if action != RULE_SNIPPET_ACTION_NAME
        }

    def _create_loop_unhappy_lookup_from_states(
        self,
        trackers_as_states: List[List[State]],
        trackers_as_actions: List[List[Text]],
    ) -> Dict[Text, Text]:
        """Creates lookup dictionary from the tracker represented as states.

        Args:
            trackers_as_states: representation of the trackers as a list of states
            trackers_as_actions: representation of the trackers as a list of actions

        Returns:
            lookup dictionary
        """

        lookup = {}
        for states, actions in zip(trackers_as_states, trackers_as_actions):
            action = actions[0]
            active_loop = get_active_loop_name(states[-1])
            # even if there are two identical feature keys
            # their loop will be the same
            if not active_loop:
                continue

            states = self._states_for_unhappy_loop_predictions(states)
            feature_key = self._create_feature_key(states)
            if not feature_key:
                continue

            # Since rule snippets and stories inside the loop contain
            # only unhappy paths, notify the loop that
            # it was predicted after an answer to a different question and
            # therefore it should not validate user input
            if (
                # loop is predicted after action_listen in unhappy path,
                # therefore no validation is needed
                is_prev_action_listen_in_state(states[-1])
                and action == active_loop
            ):
                lookup[feature_key] = DO_NOT_VALIDATE_LOOP
            elif (
                # some action other than active_loop is predicted in unhappy path,
                # therefore active_loop shouldn't be predicted by the rule
                not is_prev_action_listen_in_state(states[-1])
                and action != active_loop
            ):
                lookup[feature_key] = DO_NOT_PREDICT_LOOP_ACTION
        return lookup

    def _check_rule_restriction(
        self, rule_trackers: List[TrackerWithCachedStates]
    ) -> None:
        rules_exceeding_max_user_turns = []
        for tracker in rule_trackers:
            number_of_user_uttered = sum(
                isinstance(event, UserUttered) for event in tracker.events
            )
            if number_of_user_uttered > self.ALLOWED_NUMBER_OF_USER_INPUTS:
                rules_exceeding_max_user_turns.append(tracker.sender_id)

        if rules_exceeding_max_user_turns:
            raise InvalidRule(
                f"Found rules '{', '.join(rules_exceeding_max_user_turns)}' "
                f"that contain more than {self.ALLOWED_NUMBER_OF_USER_INPUTS} "
                f"user message. Rules are not meant to hardcode a state machine. "
                f"Please use stories for these cases."
            )

    def _predict_next_action(
        self,
        tracker: TrackerWithCachedStates,
        domain: Domain,
        interpreter: NaturalLanguageInterpreter,
    ) -> Optional[Text]:
        probabilities = self.predict_action_probabilities(tracker, domain, interpreter)
        # do not raise an error if RulePolicy didn't predict anything for stories;
        # however for rules RulePolicy should always predict an action
        predicted_action_name = None
        if (
            probabilities != self._default_predictions(domain)
            or tracker.is_rule_tracker
        ):
            predicted_action_name = domain.action_names[np.argmax(probabilities)]

        return predicted_action_name

    def _check_prediction(
        self,
        tracker: TrackerWithCachedStates,
        domain: Domain,
        interpreter: NaturalLanguageInterpreter,
        gold_action_name: Text,
    ) -> Optional[Text]:

        predicted_action_name = self._predict_next_action(tracker, domain, interpreter)
        if not predicted_action_name or predicted_action_name == gold_action_name:
            return None

        # RulePolicy will always predict active_loop first,
        # but inside loop unhappy path there might be another action
        if predicted_action_name == tracker.active_loop_name:
            rasa.core.test.emulate_loop_rejection(tracker)
            predicted_action_name = self._predict_next_action(
                tracker, domain, interpreter
            )
            if not predicted_action_name or predicted_action_name == gold_action_name:
                return None

        tracker_type = "rule" if tracker.is_rule_tracker else "story"
        return (
            f"- the prediction of the action '{gold_action_name}' in {tracker_type} "
            f"'{tracker.sender_id}' is contradicting with another rule or story."
        )

    def _find_contradicting_rules(
        self,
        trackers: List[TrackerWithCachedStates],
        domain: Domain,
        interpreter: NaturalLanguageInterpreter,
    ) -> None:
        logger.debug("Started checking rules and stories for contradictions.")
        # during training we run `predict_action_probabilities` to check for
        # contradicting rules.
        # We silent prediction debug to avoid too many logs during these checks.
        logger_level = logger.level
        logger.setLevel(logging.WARNING)
        error_messages = []
        pbar = tqdm(
            trackers,
            desc="Processed trackers",
            disable=rasa.shared.utils.io.is_logging_disabled(),
        )
        for tracker in pbar:
            running_tracker = tracker.init_copy()
            running_tracker.sender_id = tracker.sender_id
            # the first action is always unpredictable
            next_action_is_unpredictable = True
            for event in tracker.applied_events():
                if not isinstance(event, ActionExecuted):
                    running_tracker.update(event)
                    continue

                if event.action_name == RULE_SNIPPET_ACTION_NAME:
                    # notify that we shouldn't check that the action after
                    # RULE_SNIPPET_ACTION_NAME is unpredictable
                    next_action_is_unpredictable = True
                    # do not add RULE_SNIPPET_ACTION_NAME event
                    continue

                # do not run prediction on unpredictable actions
                if next_action_is_unpredictable or event.unpredictable:
                    next_action_is_unpredictable = False  # reset unpredictability
                    running_tracker.update(event)
                    continue

                gold_action_name = event.action_name or event.action_text
                error_message = self._check_prediction(
                    running_tracker, domain, interpreter, gold_action_name
                )
                if error_message:
                    error_messages.append(error_message)

                running_tracker.update(event)

        logger.setLevel(logger_level)  # reset logger level
        if error_messages:
            error_messages = "\n".join(error_messages)
            raise InvalidRule(
                f"\nContradicting rules or stories found 🚨\n\n{error_messages}\n"
                f"Please update your stories and rules so that they don't contradict "
                f"each other."
            )

        logger.debug("Found no contradicting rules.")

    def train(
        self,
        training_trackers: List[TrackerWithCachedStates],
        domain: Domain,
        interpreter: NaturalLanguageInterpreter,
        **kwargs: Any,
    ) -> None:

        # only consider original trackers (no augmented ones)
        training_trackers = [
            t
            for t in training_trackers
            if not hasattr(t, "is_augmented") or not t.is_augmented
        ]
        # only use trackers from rule-based training data
        rule_trackers = [t for t in training_trackers if t.is_rule_tracker]
        if self._restrict_rules:
            self._check_rule_restriction(rule_trackers)

        (
            rule_trackers_as_states,
            rule_trackers_as_actions,
        ) = self.featurizer.training_states_and_actions(rule_trackers, domain)

        rules_lookup = self._create_lookup_from_states(
            rule_trackers_as_states, rule_trackers_as_actions
        )
        self.lookup[RULES] = self._remove_rule_snippet_predictions(rules_lookup)

        story_trackers = [t for t in training_trackers if not t.is_rule_tracker]
        (
            story_trackers_as_states,
            story_trackers_as_actions,
        ) = self.featurizer.training_states_and_actions(story_trackers, domain)

        # use all trackers to find negative rules in unhappy paths
        trackers_as_states = rule_trackers_as_states + story_trackers_as_states
        trackers_as_actions = rule_trackers_as_actions + story_trackers_as_actions

        # negative rules are not anti-rules, they are auxiliary to actual rules
        self.lookup[
            RULES_FOR_LOOP_UNHAPPY_PATH
        ] = self._create_loop_unhappy_lookup_from_states(
            trackers_as_states, trackers_as_actions
        )

        # make this configurable because checking might take a lot of time
        if self._check_for_contradictions:
            # using trackers here might not be the most efficient way, however
            # it allows us to directly test `predict_action_probabilities` method
            self._find_contradicting_rules(training_trackers, domain, interpreter)

        logger.debug(f"Memorized '{len(self.lookup[RULES])}' unique rules.")

    @staticmethod
    def _does_rule_match_state(rule_state: State, conversation_state: State) -> bool:
        for state_type, rule_sub_state in rule_state.items():
            conversation_sub_state = conversation_state.get(state_type, {})
            for key, value in rule_sub_state.items():
                if isinstance(value, list):
                    # json dumps and loads tuples as lists,
                    # so we need to convert them back
                    value = tuple(value)

                if (
                    # value should be set, therefore
                    # check whether it is the same as in the state
                    value
                    and value != SHOULD_NOT_BE_SET
                    and conversation_sub_state.get(key) != value
                ) or (
                    # value shouldn't be set, therefore
                    # it should be None or non existent in the state
                    value == SHOULD_NOT_BE_SET
                    and conversation_sub_state.get(key)
                    # during training `SHOULD_NOT_BE_SET` is provided. Hence, we also
                    # have to check for the value of the slot state
                    and conversation_sub_state.get(key) != SHOULD_NOT_BE_SET
                ):
                    return False

        return True

    @staticmethod
    def _rule_key_to_state(rule_key: Text) -> List[State]:
        return json.loads(rule_key)

    def _is_rule_applicable(
        self, rule_key: Text, turn_index: int, conversation_state: State
    ) -> bool:
        """Check if rule is satisfied with current state at turn."""

        # turn_index goes back in time
        reversed_rule_states = list(reversed(self._rule_key_to_state(rule_key)))

        return bool(
            # rule is shorter than current turn index
            turn_index >= len(reversed_rule_states)
            # current rule and state turns are empty
            or (not reversed_rule_states[turn_index] and not conversation_state)
            # check that current rule turn features are present in current state turn
            or (
                reversed_rule_states[turn_index]
                and conversation_state
                and self._does_rule_match_state(
                    reversed_rule_states[turn_index], conversation_state
                )
            )
        )

    def _get_possible_keys(
        self, lookup: Dict[Text, Text], states: List[State]
    ) -> Set[Text]:
        possible_keys = set(lookup.keys())
        for i, state in enumerate(reversed(states)):
            # find rule keys that correspond to current state
            possible_keys = set(
                filter(
                    lambda _key: self._is_rule_applicable(_key, i, state), possible_keys
                )
            )
        return possible_keys

    @staticmethod
    def _find_action_from_default_actions(
        tracker: DialogueStateTracker,
    ) -> Optional[Text]:
        if (
            not tracker.latest_action_name == ACTION_LISTEN_NAME
            or not tracker.latest_message
        ):
            return None

        default_action_name = DEFAULT_ACTION_MAPPINGS.get(
            tracker.latest_message.intent.get(INTENT_NAME_KEY)
        )

        if default_action_name:
            logger.debug(f"Predicted default action '{default_action_name}'.")

        return default_action_name

    @staticmethod
    def _find_action_from_loop_happy_path(
        tracker: DialogueStateTracker,
    ) -> Optional[Text]:

        active_loop_name = tracker.active_loop_name
        active_loop_rejected = tracker.active_loop.get(LOOP_REJECTED)
        should_predict_loop = (
            active_loop_name
            and not active_loop_rejected
            and tracker.latest_action.get(ACTION_NAME) != active_loop_name
        )
        should_predict_listen = (
            active_loop_name
            and not active_loop_rejected
            and tracker.latest_action_name == active_loop_name
        )

        if should_predict_loop:
            logger.debug(f"Predicted loop '{active_loop_name}'.")
            return active_loop_name

        # predict `action_listen` if loop action was run successfully
        if should_predict_listen:
            logger.debug(
                f"Predicted '{ACTION_LISTEN_NAME}' after loop '{active_loop_name}'."
            )
            return ACTION_LISTEN_NAME

    def _find_action_from_rules(
        self, tracker: DialogueStateTracker, domain: Domain
    ) -> Optional[Text]:
        tracker_as_states = self.featurizer.prediction_states([tracker], domain)
        states = tracker_as_states[0]

        logger.debug(f"Current tracker state: {states}")

        rule_keys = self._get_possible_keys(self.lookup[RULES], states)
        predicted_action_name = None
        best_rule_key = ""
        if rule_keys:
            # if there are several rules,
            # it should mean that some rule is a subset of another rule
            # therefore we pick a rule of maximum length
            best_rule_key = max(rule_keys, key=len)
            predicted_action_name = self.lookup[RULES].get(best_rule_key)

        active_loop_name = tracker.active_loop_name
        if active_loop_name:
            # find rules for unhappy path of the loop
            loop_unhappy_keys = self._get_possible_keys(
                self.lookup[RULES_FOR_LOOP_UNHAPPY_PATH], states
            )
            # there could be several unhappy path conditions
            unhappy_path_conditions = [
                self.lookup[RULES_FOR_LOOP_UNHAPPY_PATH].get(key)
                for key in loop_unhappy_keys
            ]

            # Check if a rule that predicted action_listen
            # was applied inside the loop.
            # Rules might not explicitly switch back to the loop.
            # Hence, we have to take care of that.
            predicted_listen_from_general_rule = (
                predicted_action_name == ACTION_LISTEN_NAME
                and not get_active_loop_name(self._rule_key_to_state(best_rule_key)[-1])
            )
            if predicted_listen_from_general_rule:
                if DO_NOT_PREDICT_LOOP_ACTION not in unhappy_path_conditions:
                    # negative rules don't contain a key that corresponds to
                    # the fact that active_loop shouldn't be predicted
                    logger.debug(
                        f"Predicted loop '{active_loop_name}' by overwriting "
                        f"'{ACTION_LISTEN_NAME}' predicted by general rule."
                    )
                    return active_loop_name

                # do not predict anything
                predicted_action_name = None

            if DO_NOT_VALIDATE_LOOP in unhappy_path_conditions:
                logger.debug("Added `FormValidation(False)` event.")
                tracker.update(LoopInterrupted(True))

        if predicted_action_name is not None:
            logger.debug(
                f"There is a rule for the next action '{predicted_action_name}'."
            )
        else:
            logger.debug("There is no applicable rule.")

        return predicted_action_name

    def predict_action_probabilities(
        self,
        tracker: DialogueStateTracker,
        domain: Domain,
        interpreter: NaturalLanguageInterpreter,
        **kwargs: Any,
    ) -> List[float]:

        result = self._default_predictions(domain)

        # Rasa Open Source default actions overrule anything. If users want to achieve
        # the same, they need to write a rule or make sure that their loop rejects
        # accordingly.
        default_action_name = self._find_action_from_default_actions(tracker)
        if default_action_name:
            return self._prediction_result(default_action_name, tracker, domain)

        # A loop has priority over any other rule.
        # The rules or any other prediction will be applied only if a loop was rejected.
        # If we are in a loop, and the loop didn't run previously or rejected, we can
        # simply force predict the loop.
        loop_happy_path_action_name = self._find_action_from_loop_happy_path(tracker)
        if loop_happy_path_action_name:
            return self._prediction_result(loop_happy_path_action_name, tracker, domain)

        rules_action_name = self._find_action_from_rules(tracker, domain)
        if rules_action_name:
            return self._prediction_result(rules_action_name, tracker, domain)

        return result

    def _default_predictions(self, domain: Domain) -> List[float]:
        result = super()._default_predictions(domain)

        if self._enable_fallback_prediction:
            result[
                domain.index_for_action(self._fallback_action_name)
            ] = self._core_fallback_threshold
        return result

    def _metadata(self) -> Dict[Text, Any]:
        return {
            "priority": self.priority,
            "lookup": self.lookup,
            "core_fallback_threshold": self._core_fallback_threshold,
            "core_fallback_action_name": self._fallback_action_name,
            "enable_fallback_prediction": self._enable_fallback_prediction,
        }

    @classmethod
    def _metadata_filename(cls) -> Text:
        return "rule_policy.json"
