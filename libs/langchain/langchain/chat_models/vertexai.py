"""Wrapper around Google VertexAI chat-based models."""
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

from langchain.callbacks.manager import CallbackManagerForLLMRun
from langchain.chat_models.base import BaseChatModel
from langchain.llms.vertexai import _VertexAICommon, is_codey_model
from langchain.pydantic_v1 import root_validator
from langchain.schema import (
    ChatGeneration,
    ChatResult,
)
from langchain.schema.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain.schema.output import ChatGenerationChunk
from langchain.utilities.vertexai import raise_vertex_import_error

if TYPE_CHECKING:
    from vertexai.language_models import ChatMessage, InputOutputTextPair


@dataclass
class _ChatHistory:
    """Represents a context and a history of messages."""

    history: List["ChatMessage"] = field(default_factory=list)
    context: Optional[str] = None


def _parse_chat_history(history: List[BaseMessage]) -> _ChatHistory:
    """Parse a sequence of messages into history.

    Args:
        history: The list of messages to re-create the history of the chat.
    Returns:
        A parsed chat history.
    Raises:
        ValueError: If a sequence of message has a SystemMessage not at the
        first place.
    """
    from vertexai.language_models import ChatMessage

    vertex_messages, context = [], None
    for i, message in enumerate(history):
        if i == 0 and isinstance(message, SystemMessage):
            context = message.content
        elif isinstance(message, AIMessage):
            vertex_message = ChatMessage(content=message.content, author="bot")
            vertex_messages.append(vertex_message)
        elif isinstance(message, HumanMessage):
            vertex_message = ChatMessage(content=message.content, author="user")
            vertex_messages.append(vertex_message)
        else:
            raise ValueError(
                f"Unexpected message with type {type(message)} at the position {i}."
            )
    chat_history = _ChatHistory(context=context, history=vertex_messages)
    return chat_history


def _parse_examples(examples: List[BaseMessage]) -> List["InputOutputTextPair"]:
    from vertexai.language_models import InputOutputTextPair

    if len(examples) % 2 != 0:
        raise ValueError(
            f"Expect examples to have an even amount of messages, got {len(examples)}."
        )
    example_pairs = []
    input_text = None
    for i, example in enumerate(examples):
        if i % 2 == 0:
            if not isinstance(example, HumanMessage):
                raise ValueError(
                    f"Expected the first message in a part to be from human, got "
                    f"{type(example)} for the {i}th message."
                )
            input_text = example.content
        if i % 2 == 1:
            if not isinstance(example, AIMessage):
                raise ValueError(
                    f"Expected the second message in a part to be from AI, got "
                    f"{type(example)} for the {i}th message."
                )
            pair = InputOutputTextPair(
                input_text=input_text, output_text=example.content
            )
            example_pairs.append(pair)
    return example_pairs


class ChatVertexAI(_VertexAICommon, BaseChatModel):
    """`Vertex AI` Chat large language models API."""

    model_name: str = "chat-bison"
    streaming: Optional[bool] = False

    @root_validator()
    def validate_environment(cls, values: Dict) -> Dict:
        """Validate that the python package exists in environment."""
        cls._try_init_vertexai(values)
        try:
            if is_codey_model(values["model_name"]):
                from vertexai.preview.language_models import CodeChatModel

                values["client"] = CodeChatModel.from_pretrained(values["model_name"])
            else:
                from vertexai.preview.language_models import ChatModel

                values["client"] = ChatModel.from_pretrained(values["model_name"])
        except ImportError:
            raise_vertex_import_error(minimum_expected_version="1.29.0")
        return values

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        stream: Optional[bool] = False,
        **kwargs: Any,
    ) -> ChatResult:
        """Generate next turn in the conversation.

        Args:
            messages: The history of the conversation as a list of messages. Code chat
                does not support context.
            stop: The list of stop words (optional).
            run_manager: The CallbackManager for LLM run, it's not used at the moment.

        Returns:
            The ChatResult that contains outputs generated by the model.

        Raises:
            ValueError: if the last message in the list is not from human.
        """
        if stream if stream is not None else self.streaming:
            return ChatResult(
                generations=list(self._stream(messages, stop, run_manager, **kwargs))
            )
        if not messages:
            raise ValueError(
                "You should provide at least one message to start the chat!"
            )
        question = messages[-1]
        if not isinstance(question, HumanMessage):
            raise ValueError(
                f"Last message in the list should be from human, got {question.type}."
            )
        history = _parse_chat_history(messages[:-1])
        context = history.context if history.context else None
        params = {**self._default_params, **kwargs}
        examples = kwargs.get("examples", None)
        if examples:
            params["examples"] = _parse_examples(examples)
        if not self.is_codey_model:
            chat = self.client.start_chat(
                context=context, message_history=history.history, **params
            )
        else:
            chat = self.client.start_chat(message_history=history.history, **params)
        response = chat.send_message(question.content)
        text = self._enforce_stop_words(response.text, stop)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        if not messages:
            raise ValueError(
                "You should provide at least one message to start the chat!"
            )
        question = messages[-1]
        if not isinstance(question, HumanMessage):
            raise ValueError(
                f"Last message in the list should be from human, got {question.type}."
            )
        history = _parse_chat_history(messages[:-1])
        context = history.context if history.context else None
        params = {**self._default_params, **kwargs}
        examples = kwargs.get("examples", None)
        if examples:
            params["examples"] = _parse_examples(examples)

        if not self.is_codey_model:
            chat = self.client.start_chat(
                context=context, message_history=history.history, **params
            )
        else:
            chat = self.client.start_chat(message_history=history.history, **params)

        responses = chat.send_message_streaming(question.content, **params)
        for response in responses:
            text = self._enforce_stop_words(response.text, stop)
            if run_manager:
                run_manager.on_llm_new_token(text)
            yield ChatGenerationChunk(message=AIMessageChunk(content=text))
