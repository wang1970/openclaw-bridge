"""TTS 语音播报引擎（sherpa-onnx 离线合成 + sounddevice 播放）"""

import logging
import re

import numpy as np
import sherpa_onnx
import sounddevice as sd

logger = logging.getLogger(__name__)


def clean_text_for_tts(text: str) -> str:
    """
    清洗文本，使其适合 TTS 合成。
    去除 emoji、markdown 格式符号等非语音内容，其余交给 TTS 模型处理。
    """
    if not text:
        return ""

    # 1. 去 emoji
    text = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U0001F900-\U0001F9FF"  # supplemental
        "\U0001FA00-\U0001FA6F"
        "\U0001FA70-\U0001FAFF"
        "\U00002702-\U000027B0"
        "\U0000FE00-\U0000FE0F"
        "\u200d"
        "]+",
        flags=re.UNICODE,
    ).sub("", text)

    # 2. 去 markdown 格式符号
    text = re.sub(r"[#*`~\[\]()>{|_\uff5e\u3000]", "", text)

    # 3. 去 markdown checklist 标记
    text = re.sub(r"\u2705|\u274c|\u2714\ufe0f|\u274e", "", text)

    # 4. 合并多余空白
    text = re.sub(r"\s+", " ", text).strip()

    return text


class TTSEngine:
    """使用 sherpa-onnx VITS 模型离线合成语音，sounddevice 播放。"""

    def __init__(self, model_dir: str, tokens: str, lexicon: str = "",
                 dict_dir: str = "", data_dir: str = "",
                 noise_scale: float = 0.667, length_scale: float = 1.0):
        vits = sherpa_onnx.OfflineTtsVitsModelConfig(
            model=f"{model_dir}/model.onnx",
            tokens=tokens,
            lexicon=lexicon,
            data_dir=data_dir,
            dict_dir=dict_dir,
            noise_scale=noise_scale,
            length_scale=length_scale,
        )
        model_config = sherpa_onnx.OfflineTtsModelConfig(
            vits=vits,
        )
        config = sherpa_onnx.OfflineTtsConfig(
            model=model_config,
            max_num_sentences=10,
        )
        self.tts = sherpa_onnx.OfflineTts(config)
        self.sample_rate = self.tts.sample_rate
        logger.info(f"TTS 引擎加载完成 (sample_rate={self.sample_rate})")

    def speak(self, text: str) -> bool:
        """同步合成并播放（阻塞直到播放完毕）。"""
        if not text or not text.strip():
            return False

        # 清洗文本
        text = clean_text_for_tts(text)

        logger.debug(f"TTS 合成文本: \"{text[:60]}\"")
        if not text:
            logger.warning("TTS 文本清洗后为空，跳过")
            return False

        try:
            audio = self.tts.generate(text)
            raw = audio.samples if audio.samples else []
            samples = np.array(raw, dtype=np.float32)
            duration = len(samples) / self.sample_rate

            if len(samples) == 0:
                logger.warning("TTS 合成结果为空")
                return False

            logger.debug(f"TTS 合成: {len(samples)} samples, {duration:.1f}s, range=[{samples.min():.3f}, {samples.max():.3f}]")

            sd.play(samples, samplerate=self.sample_rate, blocking=True)
            logger.debug("TTS 播放完成")
            return True
        except Exception as e:
            logger.error(f"TTS 异常: {e}")
            return False

    def stop(self):
        """停止播放。"""
        sd.stop()
