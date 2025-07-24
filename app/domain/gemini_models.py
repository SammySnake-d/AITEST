from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from app.core.constants import DEFAULT_TEMPERATURE, DEFAULT_TOP_K, DEFAULT_TOP_P


class SafetySetting(BaseModel):
    category: Optional[
        Literal[
            "HARM_CATEGORY_HATE_SPEECH",
            "HARM_CATEGORY_DANGEROUS_CONTENT",
            "HARM_CATEGORY_HARASSMENT",
            "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_CIVIC_INTEGRITY",
        ]
    ] = None
    threshold: Optional[
        Literal[
            "HARM_BLOCK_THRESHOLD_UNSPECIFIED",
            "BLOCK_LOW_AND_ABOVE",
            "BLOCK_MEDIUM_AND_ABOVE",
            "BLOCK_ONLY_HIGH",
            "BLOCK_NONE",
            "OFF",
        ]
    ] = None


class GenerationConfig(BaseModel):
    stopSequences: Optional[List[str]] = None
    responseMimeType: Optional[str] = None
    responseSchema: Optional[Dict[str, Any]] = None
    candidateCount: Optional[int] = 1
    maxOutputTokens: Optional[int] = None
    temperature: Optional[float] = DEFAULT_TEMPERATURE
    topP: Optional[float] = DEFAULT_TOP_P
    topK: Optional[int] = DEFAULT_TOP_K
    presencePenalty: Optional[float] = None
    frequencyPenalty: Optional[float] = None
    responseLogprobs: Optional[bool] = None
    logprobs: Optional[int] = None
    thinkingConfig: Optional[Dict[str, Any]] = None
    # TTS相关字段
    responseModalities: Optional[List[str]] = None
    speechConfig: Optional[Dict[str, Any]] = None


class SystemInstruction(BaseModel):
    role: Optional[str] = "system"
    parts: Union[List[Dict[str, Any]], Dict[str, Any]]


class GeminiContent(BaseModel):
    role: Optional[str] = None
    parts: List[Dict[str, Any]]


class GeminiRequest(BaseModel):
    contents: List[GeminiContent] = []
    tools: Optional[Union[List[Dict[str, Any]], Dict[str, Any]]] = []
    safetySettings: Optional[List[SafetySetting]] = Field(
        default=None, alias="safety_settings"
    )
    generationConfig: Optional[GenerationConfig] = Field(
        default=None, alias="generation_config"
    )
    systemInstruction: Optional[SystemInstruction] = Field(
        default=None, alias="system_instruction"
    )

    class Config:
        populate_by_name = True


class ResetSelectedKeysRequest(BaseModel):
    keys: List[str]
    key_type: str


class VerifySelectedKeysRequest(BaseModel):
    keys: List[str]


class BatchSearchKeysRequest(BaseModel):
    keys_input: str  # 支持分号、半角逗号或换行分割的密钥输入


class BatchOperationKeysRequest(BaseModel):
    keys: List[str]
    operation: Literal["enable", "disable"]  # 操作类型：启用或禁用
    key_type: Optional[str] = "gemini"  # 密钥类型：gemini 或 vertex


class KeyFreezeRequest(BaseModel):
    key: str
    duration_seconds: Optional[int] = None  # 冷冻时间，为空则使用默认配置
    key_type: Optional[str] = "gemini"  # 密钥类型：gemini 或 vertex


class KeysPaginationRequest(BaseModel):
    key_type: Literal["valid", "invalid", "disabled"] = "valid"  # 密钥类型
    page: int = Field(1, ge=1, description="页码，从1开始")
    page_size: int = Field(10, ge=1, le=1000, description="每页大小，最大1000")
    search: Optional[str] = Field(None, description="搜索关键词")
    fail_count_threshold: Optional[int] = Field(0, ge=0, description="失败次数阈值（仅对valid类型有效）")


class KeysPaginationResponse(BaseModel):
    success: bool
    data: Dict[str, Any]  # 包含keys和分页信息
    total_count: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool
