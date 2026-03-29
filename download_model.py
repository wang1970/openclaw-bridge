"""下载 sherpa-onnx 预训练模型"""

import os
import sys
import tarfile
import urllib.request

# ASR 模型 (paraformer-zh-small)
ASR_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-paraformer-zh-small-2024-03-09.tar.bz2"
ASR_DIR = "./models/sherpa-onnx-paraformer-zh-small-2024-03-09"

# KWS 唤醒词模型 (wenetspeech 中文)
KWS_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2"
KWS_DIR = "./models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"

# TTS 语音合成模型 (MeloTTS 中英双语版)
TTS_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-melo-tts-zh_en.tar.bz2"
TTS_DIR = "./models/vits-melo-tts-zh_en"


def download(url: str, dest: str):
    print(f"下载: {url}")
    urllib.request.urlretrieve(url, dest, reporthook=_progress)
    print()


def _progress(block_num, block_size, total_size):
    done = block_num * block_size
    if total_size > 0:
        pct = min(done * 100 // total_size, 100)
        mb_done = done / 1024 / 1024
        mb_total = total_size / 1024 / 1024
        print(f"\r  {mb_done:.1f}/{mb_total:.1f} MB ({pct}%)", end="", flush=True)


def extract(archive: str, target_dir: str):
    print(f"解压: {archive} → {target_dir}")
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall("./models/")


def main():
    os.makedirs("./models", exist_ok=True)

    # 1. ASR 模型
    if os.path.exists(ASR_DIR):
        print(f"[跳过] ASR 模型已存在: {ASR_DIR}")
    else:
        archive = "./models/asr-model.tar.bz2"
        download(ASR_URL, archive)
        extract(archive, ASR_DIR)
        os.remove(archive)
        print(f"[完成] ASR 模型: {ASR_DIR}")

    # 2. KWS 模型
    if os.path.exists(KWS_DIR):
        print(f"[跳过] KWS 模型已存在: {KWS_DIR}")
    else:
        archive = "./models/kws-model.tar.bz2"
        download(KWS_URL, archive)
        extract(archive, KWS_DIR)
        os.remove(archive)
        print(f"[完成] KWS 模型: {KWS_DIR}")

    # 3. TTS 模型
    if os.path.exists(TTS_DIR):
        print(f"[跳过] TTS 模型已存在: {TTS_DIR}")
    else:
        archive = "./models/tts-model.tar.bz2"
        download(TTS_URL, archive)
        extract(archive, TTS_DIR)
        os.remove(archive)
        print(f"[完成] TTS 模型: {TTS_DIR}")

    print("\n所有模型下载完成！")


if __name__ == "__main__":
    main()
