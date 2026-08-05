import json
import logging
from typing import Callable, TypeVar, Optional
from app.agents.tools.llm_client import LLMClient, LLMClientError

T = TypeVar("T")

logger = logging.getLogger(__name__)


def truncate_at_word_boundary(text: str, char_limit: int) -> str:
    """
    Truncate text to char_limit characters without cutting mid-word.
    Finds the last space before the limit and cuts there.
    Appends '... [truncated]' so the model knows the body was cut.
    """
    if len(text) <= char_limit:
        return text

    # Find the last space before the character limit
    truncated = text[:char_limit]
    last_space = truncated.rfind(" ")

    if last_space == -1:
        # No space found — very long single word, cut at limit
        return truncated + "... [truncated]"

    return truncated[:last_space] + "... [truncated]"


def clean_and_parse_json(raw_response: str, logger: logging.Logger) -> dict:
    """
    Parse the LLM's raw text response into a Python dict.
    Handles common issues with local LLM output:
    - Markdown code fences (```json ... ```)
    - Leading/trailing whitespace or text
    - Slightly malformed JSON
    """
    if not raw_response or not raw_response.strip():
        logger.warning("LLM returned empty response.")
        return {}

    text = raw_response.strip()

    # Strip markdown code fences if present
    if "```" in text:
        lines = text.split("\n")
        lines = [
            line for line in lines
            if not line.strip().startswith("```")
        ]
        text = "\n".join(lines).strip()

    # Extract JSON object — find first { and last }
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:
        logger.warning(
            f"No JSON object found in LLM response: {text[:200]}"
        )
        return {}

    json_str = text[start:end + 1]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse LLM JSON response: {e}. Raw: {json_str[:200]}")
        return {}


def safe_enum(enum_class, value, default, field_name: str, email_id: str, logger: logging.Logger):
    """
    Safely convert a string value to an enum member.
    Returns the default if the value is None, empty, or not a valid enum value.
    """
    if not value:
        logger.warning(
            f"Missing '{field_name}' in LLM response for email {email_id}. "
            f"Defaulting to {default.value}."
        )
        return default

    try:
        return enum_class(value)
    except ValueError:
        logger.warning(
            f"Invalid '{field_name}' value '{value}' for email {email_id}. "
            f"Valid values: {[e.value for e in enum_class]}. "
            f"Defaulting to {default.value}."
        )
        return default


def safe_bool(value, default: bool = False) -> bool:
    """Safely convert LLM output to bool (handles true/false strings)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes"):
            return True
        if normalized in ("false", "0", "no", ""):
            return False
    return bool(value)


def safe_int(value, default: int) -> int:
    """Safely convert a value to int, returning default on failure."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def run_llm_agent_tool(
    system_prompt: str,
    user_prompt: str,
    parser_fn: Callable[[dict], T],
    fallback_fn: Callable[[], T],
    llm_client: Optional[LLMClient] = None,
    llm_model: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> T:
    """
    Base function for executing LLM-based agent tools.
    Handles LLMClient initialization, completing the prompt, catching errors,
    cleaning/parsing responses, and fallback execution.
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    if llm_client is None:
        llm_client = LLMClient(model=llm_model)

    try:
        raw_response = llm_client.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=llm_model,
        )

        parsed = clean_and_parse_json(raw_response, logger)
        if not parsed:
            logger.warning("Parsing parsed dictionary failed or returned empty. Invoking fallback.")
            return fallback_fn()

        return parser_fn(parsed)

    except LLMClientError as e:
        logger.warning(
            f"LLM call failed for tool execution: {e}. "
            f"Invoking fallback."
        )
        return fallback_fn()

    except Exception as e:
        logger.error(
            f"Unexpected error during tool execution: {e}. "
            f"Invoking fallback."
        )
        return fallback_fn()
