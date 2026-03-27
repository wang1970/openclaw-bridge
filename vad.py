"""基于音频能量的 VAD 实现（无需编译）"""

import logging
from dataclasses import dataclass, field

import numpy as np

from config import CHUNK_MS, CHUNK_SIZE, SAMPLE_RATE

logger = logging.getLogger(__name__)

# 能量阈值：RMS 低于此值视为静音
DEFAULT_SILENCE_THRESHOLD = 0.015


@dataclass
class VADResult:
    """VAD 处理结果"""
    is_speech: bool = False
    speech_started: bool = False
    speech_ended: bool = False
    speech_frames: int = 0
    silence_frames: int = 0
    audio_buffer: list = field(default_factory=list)


class VADProcessor:
    def __init__(
        self,
        aggressiveness: int = 2,
        speech_threshold: int = 5,
        silence_threshold: int = 15,
        silence_rms: float = DEFAULT_SILENCE_THRESHOLD,
        pre_buffer_frames: int = 10,
    ):
        self.speech_threshold = speech_threshold
        self.silence_threshold = silence_threshold
        self.pre_buffer_frames = pre_buffer_frames
        # aggressiveness 越大 → 阈值越高 → 越严格
        rms_map = {0: 0.005, 1: 0.010, 2: 0.015, 3: 0.025}
        self.silence_rms = rms_map.get(aggressiveness, silence_rms)

        self.is_speaking = False
        self.continuous_speech = 0
        self.continuous_silence = 0
        self.audio_buffer: list[bytes] = []
        self.pre_buffer: list[bytes] = []

        logger.debug(
            f"VAD 初始化: 静音阈值 RMS={self.silence_rms}, "
            f"语音起始帧={speech_threshold}, 静音结束帧={silence_threshold}, "
            f"预缓冲帧={pre_buffer_frames}({pre_buffer_frames * CHUNK_MS}ms)"
        )

    def _compute_rms(self, frame_bytes: bytes) -> float:
        """计算 PCM int16 音频的 RMS 能量"""
        if len(frame_bytes) < 2:
            return 0.0
        samples = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(samples ** 2)) / 32768.0)
        return rms

    def _is_speech_frame(self, frame_bytes: bytes) -> bool:
        return self._compute_rms(frame_bytes) > self.silence_rms

    def process_frame(self, frame_bytes: bytes) -> VADResult:
        """处理一帧音频"""
        result = VADResult()

        is_speech = self._is_speech_frame(frame_bytes)
        result.is_speech = is_speech

        # 始终维护预缓冲区（在非说话状态时）
        if not self.is_speaking:
            self.pre_buffer.append(frame_bytes)
            if len(self.pre_buffer) > self.pre_buffer_frames:
                self.pre_buffer.pop(0)

        if is_speech:
            self.continuous_speech += 1
            self.continuous_silence = 0
        else:
            self.continuous_silence += 1
            self.continuous_speech = 0

        # 状态转换
        if not self.is_speaking:
            if self.continuous_speech >= self.speech_threshold:
                self.is_speaking = True
                self.continuous_speech = 0
                self.continuous_silence = 0
                result.speech_started = True
                # 把预缓冲区的音频插入到录音缓冲区前面
                self.audio_buffer = list(self.pre_buffer)
                self.pre_buffer = []
        else:
            if self.continuous_silence >= self.silence_threshold:
                self.is_speaking = False
                self.continuous_speech = 0
                self.continuous_silence = 0
                result.speech_ended = True

        # 累积音频（正在说话 或 刚开始说话 时累积）
        if self.is_speaking or result.speech_started:
            self.audio_buffer.append(frame_bytes)

        result.speech_frames = self.continuous_speech
        result.silence_frames = self.continuous_silence

        if result.speech_ended:
            # 裁剪尾部静音帧：从后往前找，去掉连续的静音帧
            trimmed = list(self.audio_buffer)
            while trimmed and not self._is_speech_frame(trimmed[-1]):
                trimmed.pop()
            result.audio_buffer = trimmed
            self.audio_buffer = []

        return result

    def reset(self):
        """重置状态"""
        self.is_speaking = False
        self.continuous_speech = 0
        self.continuous_silence = 0
        self.audio_buffer = []
        self.pre_buffer = []


def pcm_bytes_to_float32(pcm_bytes_list: list) -> np.ndarray:
    """将 PCM int16 字节列表转换为 float32 numpy 数组"""
    if not pcm_bytes_list:
        return np.array([], dtype=np.float32)
    raw = b"".join(pcm_bytes_list)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    return samples / 32768.0
