"""sherpa-onnx 离线 ASR 引擎封装"""

import logging
from pathlib import Path

import numpy as np
import sherpa_onnx

from config import SAMPLE_RATE

logger = logging.getLogger(__name__)


class ASREngine:
    def __init__(self, model_dir: str):
        model_path = Path(model_dir)
        if not model_path.exists():
            raise FileNotFoundError(
                f"模型目录不存在: {model_dir}\n"
                f"请运行 python download_model.py 下载模型"
            )

        # 查找模型文件
        tokens_path = model_path / "tokens.txt"

        # 检测可用的模型文件
        paraformer_path = model_path / "model.int8.onnx"
        if not paraformer_path.exists():
            paraformer_path = model_path / "model.onnx"

        if not tokens_path.exists():
            raise FileNotFoundError(f"tokens.txt 不存在: {tokens_path}")
        if not paraformer_path.exists():
            raise FileNotFoundError(
                f"找不到 paraformer 模型文件 (model.onnx / model.int8.onnx)"
            )

        logger.debug(f"加载 ASR 模型: {model_path}")
        logger.debug(f"  tokens: {tokens_path}")
        logger.debug(f"  model:  {paraformer_path}")

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
            paraformer=str(paraformer_path),
            tokens=str(tokens_path),
            num_threads=2,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            debug=False,
        )
        logger.info("ASR 模型加载完成")

    def recognize(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        """
        识别一段音频，返回文本。

        Args:
            audio: float32 数组，范围 [-1, 1]
            sample_rate: 采样率
        """
        # 最小音频时长保护（< 0.3s 的片段无法产生有效特征）
        if len(audio) < int(0.3 * SAMPLE_RATE):
            return ""

        # 采样率转换
        if sample_rate != SAMPLE_RATE:
            audio = self._resample(audio, sample_rate, SAMPLE_RATE)

        try:
            stream = self.recognizer.create_stream()
            stream.accept_waveform(SAMPLE_RATE, audio)
            self.recognizer.decode_stream(stream)
            text = stream.result.text.strip()
            return text
        except Exception as e:
            logger.debug(f"ASR 识别异常（音频长度={len(audio)}）: {e}")
            return ""

    @staticmethod
    def _resample(audio: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
        """简单的线性重采样"""
        if from_rate == to_rate:
            return audio
        ratio = to_rate / from_rate
        new_length = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, new_length)
        return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)
