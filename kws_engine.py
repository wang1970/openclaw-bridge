"""sherpa-onnx KWS 专用唤醒词检测引擎"""

import logging
from pathlib import Path

import numpy as np
import sherpa_onnx

from config import KWS_MODEL_DIR, KWS_KEYWORDS, KWS_KEYWORDS_THRESHOLD, SAMPLE_RATE

logger = logging.getLogger(__name__)


class KWSEngine:
    """关键词检测引擎（流式，极低延迟）"""

    def __init__(self, model_dir: str = KWS_MODEL_DIR):
        model_path = Path(model_dir)
        if not model_path.exists():
            raise FileNotFoundError(
                f"KWS 模型目录不存在: {model_dir}\n"
                f"请运行 python download_model.py 下载模型"
            )

        tokens_path = model_path / "tokens.txt"
        encoder_path = model_path / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        decoder_path = model_path / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        joiner_path = model_path / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"

        # 回退到非 int8
        if not encoder_path.exists():
            encoder_path = model_path / "encoder-epoch-12-avg-2-chunk-16-left-64.onnx"
        if not decoder_path.exists():
            decoder_path = model_path / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"
        if not joiner_path.exists():
            joiner_path = model_path / "joiner-epoch-12-avg-2-chunk-16-left-64.onnx"

        for p in [tokens_path, encoder_path, decoder_path, joiner_path]:
            if not p.exists():
                raise FileNotFoundError(f"KWS 模型文件缺失: {p}")

        # 生成 keywords 文件
        keywords_file = model_path / "keywords_custom.txt"
        self._write_keywords_file(keywords_file, KWS_KEYWORDS)

        logger.debug(f"加载 KWS 唤醒词模型: {model_path}")
        logger.debug(f"  encoder: {encoder_path.name}")
        logger.debug(f"  keywords: {KWS_KEYWORDS}")

        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(tokens_path),
            encoder=str(encoder_path),
            decoder=str(decoder_path),
            joiner=str(joiner_path),
            keywords_file=str(keywords_file),
            num_threads=2,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            keywords_threshold=KWS_KEYWORDS_THRESHOLD,
            provider="cpu",
        )

        self.stream = self.kws.create_stream()
        logger.info("KWS 模型加载完成")

    @staticmethod
    def _write_keywords_file(path: Path, keywords: list[str]):
        """
        生成 keywords.txt 文件。
        格式: 拼音token（空格分隔） @显示文本
        例: x iǎo l óng x iā @小龙虾
        """
        # 汉字 → 拼音映射（只覆盖需要的字）
        pinyin_map = {
            "小": "x iǎo",
            "龙": "l óng",
            "虾": "x iā",
            "你": "n ǐ",
            "好": "h ǎo",
            "同": "t óng",
            "学": "x ué",
            "米": "m ǐ",
            "爱": "ài",
            "艺": "y ì",
        }

        lines = []
        for kw in keywords:
            syllables = []
            for char in kw:
                if char in pinyin_map:
                    syllables.extend(pinyin_map[char].split(" "))
                else:
                    logger.warning(f"KWS: 汉字「{char}」不在拼音映射中，跳过")
            if syllables:
                lines.append(" ".join(syllables) + f" @{kw}")

        content = "\n".join(lines) + "\n"
        path.write_text(content, encoding="utf-8")

    def feed(self, samples: np.ndarray) -> bool:
        """
        喂入一帧音频（float32, [-1, 1]）。
        返回 True 表示检测到唤醒词。
        """
        self.stream.accept_waveform(SAMPLE_RATE, samples)
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
        result = self.kws.get_result(self.stream)
        if result:
            logger.debug(f"[KWS] 检测到关键词: {result.strip()}")
            self.kws.reset_stream(self.stream)
            return True
        return False
