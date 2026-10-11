"""Типы параметров инструментов: из них MCP строит JSON-схему, которую видит агент."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

ShortOption = Annotated[str, Field(min_length=1, max_length=40)]


class QuestionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[str, Field(min_length=1, max_length=500, description="Текст вопроса")]
    options: Annotated[
        list[ShortOption],
        Field(max_length=6, description="Варианты ответа — станут кнопками"),
    ] = []
    allow_free_text: Annotated[
        bool, Field(description="Можно ли ответить своим текстом, а не только вариантом")
    ] = True


Questions = Annotated[list[QuestionItem], Field(min_length=1, max_length=5)]
