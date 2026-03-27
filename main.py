"""
OpenClaw Bridge - 主程序

双线程架构：
  - 音频采集线程 (Producer)：麦克风 → VAD 分段 + KWS 唤醒检测 → 事件入队列
  - 策略处理线程 (Consumer)：从队列取事件 → ASR 识别 → 状态机决策 → 提交命令

唤醒方式（双引擎）：
  1. KWS 专用唤醒词引擎（主要）：实时流式检测，低延迟低 CPU
  2. ASR 文本匹配（后备）：VAD 截取语音段 → ASR 识别 → 文本匹配唤醒词

用法:
  1. python download_model.py    # 首次运行，下载模型
  2. python main.py              # 启动
  3. 说话 "小龙虾"               # 唤醒
  4. 说出指令内容                 # 识别后提交给 OpenClaw
"""

import argparse
import logging
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto

import numpy as np
import sounddevice as sd

import config
from asr_engine import ASREngine
from openclaw_client import OpenClawClient
from vad import VADProcessor, pcm_bytes_to_float32

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 控制台默认只显示 INFO+，通过 --debug 切换为 DEBUG+
logging.getLogger().handlers[0].setLevel(logging.INFO)

# TTS 引擎（可选）
_tts = None
if config.TTS_ENABLED:
    try:
        from tts_engine import TTSEngine
        model_dir = config.TTS_MODEL_DIR
        tokens = config.TTS_TOKENS or f"{model_dir}/tokens.txt"
        lexicon = config.TTS_LEXICON or f"{model_dir}/lexicon.txt"
        _tts = TTSEngine(
            model_dir=model_dir,
            tokens=tokens,
            lexicon=lexicon,
            length_scale=config.TTS_LENGTH_SCALE,
            keep_english=config.TTS_KEEP_ENGLISH,
        )
    except Exception as e:
        logger.warning(f"TTS 初始化失败，语音播报不可用: {e}")


# ─── 队列事件类型 ─────────────────────────────────────────

@dataclass
class WakeEvent:
    """KWS 唤醒事件"""
    source: str = "kws"


@dataclass
class UtteranceEvent:
    """语音片段事件"""
    audio_buffer: list


@dataclass
class AgentResponseEvent:
    """Agent 响应事件（TTS 播报用）"""
    text: str


# ─── 策略线程状态机 ───────────────────────────────────────

class State(Enum):
    IDLE = auto()           # 空闲监听（等待唤醒词）
    WAKE_DETECTED = auto()  # 唤醒成功，等待用户说出命令
    LISTENING_CMD = auto()  # 正在录制命令（可能多段语音拼接）
    PLAYING = auto()        # TTS 播放中（麦克风暂停）
    FOLLOWUP = auto()       # 多轮后续侦听（Agent 回答后，用户可直接追问）


class VoiceBridge:
    def __init__(self):
        self.running = True

        # 事件队列：音频线程 → 策略线程
        self.event_queue: queue.Queue = queue.Queue()

        # 麦克风流引用（用于播放时暂停/恢复）
        self.mic_stream = None

        # 策略线程状态（只在策略线程中读写，无需锁）
        self.state = State.IDLE
        self.last_wake_time = 0.0
        self.cmd_buffer: list[str] = []
        self.last_utterance_time = 0.0

        # 回复等待：快速回复正常流，慢回复时缓冲新消息
        self._waiting_response = False
        self._pending_cmd = ""  # 缓冲的一条完整命令

        # ASR 引擎
        self.asr = ASREngine(config.MODEL_DIR)

        # KWS 唤醒词引擎（可选）
        self.kws = None
        if config.KWS_ENABLED:
            try:
                from kws_engine import KWSEngine
                self.kws = KWSEngine()
            except Exception as e:
                logger.warning(f"KWS 加载失败，回退到纯 ASR 模式: {e}")

        # OpenClaw 客户端
        self.client = OpenClawClient(on_response=self._on_agent_response)

        # 启动策略处理线程
        self.strategy_thread = threading.Thread(
            target=self._strategy_loop, daemon=True, name="strategy"
        )
        self.strategy_thread.start()

        logger.info("=" * 50)
        logger.info("OpenClaw Bridge 已启动")
        logger.info(f"唤醒词: {config.WAKE_WORD} | 引擎: {'KWS+ASR' if self.kws else 'ASR'}")
        logger.info(f"网关: {config.OPENCLAW_URL}")
        logger.info("=" * 50)

    # ─── 音频采集线程（Producer）──────────────────────────────

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status):
        """
        音频回调（在 sounddevice 的音频线程中运行）。
        职责：VAD 分段 + KWS 唤醒检测 → 事件入队列。必须快速返回。
        """
        if status:
            logger.warning(f"音频状态: {status}")

        float32_samples = indata[:, 0]

        # KWS 实时唤醒检测（极轻量，3M 模型）
        if self.kws:
            try:
                detected = self.kws.feed(float32_samples)
                if detected:
                    self.event_queue.put(WakeEvent())
            except Exception as e:
                logger.error(f"KWS 检测异常: {e}")

        # VAD 分段（能量检测）
        samples_int16 = (float32_samples * 32768).astype(np.int16)
        frame_bytes = samples_int16.tobytes()

        vad_result = self.vad.process_frame(frame_bytes)

        # 语音结束 → 产出一条完整语音片段
        if vad_result.speech_ended and vad_result.audio_buffer:
            self.event_queue.put(UtteranceEvent(audio_buffer=vad_result.audio_buffer))

    # ─── 策略处理线程（Consumer）──────────────────────────────

    def _strategy_loop(self):
        """策略线程主循环：从队列取事件，ASR 识别，状态机决策。"""
        while self.running:
            try:
                event = self.event_queue.get(timeout=0.5)
            except queue.Empty:
                self._check_timeouts()
                continue

            try:
                if isinstance(event, WakeEvent):
                    self._handle_wake_event(event)
                elif isinstance(event, UtteranceEvent):
                    self._process_utterance(event.audio_buffer)
                elif isinstance(event, AgentResponseEvent):
                    self._handle_agent_response(event)
            except Exception as e:
                logger.error(f"策略处理异常: {e}")

            self.event_queue.task_done()

    def _check_timeouts(self):
        """检查是否触发超时"""
        now = time.time()

        if self.state == State.WAKE_DETECTED:
            if now - self.last_utterance_time > config.CMD_TIMEOUT:
                logger.info("等待命令超时，回到空闲监听")
                self._speak("小龙虾跑了，再说一次小龙虾叫它回来")
                self._goto_idle()

        elif self.state == State.LISTENING_CMD:
            if now - self.last_utterance_time > config.CMD_END_SILENCE:
                self._finish_command()

        elif self.state == State.FOLLOWUP:
            if now - self.last_utterance_time > config.FOLLOWUP_TIMEOUT:
                logger.info("后续侦听超时，回到空闲监听")
                self._goto_idle()

    # ─── KWS 唤醒事件处理 ───────────────────────────────────

    def _handle_wake_event(self, event: WakeEvent):
        """KWS 检测到唤醒词"""
        now = time.time()

        if self.state in (State.LISTENING_CMD, State.PLAYING):
            return

        # 所有状态都需要冷却检查
        if now - self.last_wake_time < config.WAKE_COOLDOWN:
            logger.info("唤醒冷却中")
            return

        self.last_wake_time = now
        logger.info(f">>> 唤醒「{config.WAKE_WORD}」！")

        self.state = State.WAKE_DETECTED
        self.cmd_buffer = []
        self.last_utterance_time = now

    # ─── 语音片段处理 ───────────────────────────────────────

    def _process_utterance(self, audio_buffer: list):
        """处理一条语音片段（策略线程中执行）"""
        audio = pcm_bytes_to_float32(audio_buffer)
        if len(audio) < 0.3 * config.SAMPLE_RATE:
            return

        try:
            text = self.asr.recognize(audio)
        except Exception as e:
            logger.debug(f"[ASR] 识别异常（音频过短或无效）: {e}")
            return
        if not text:
            return

        # 单字噪音过滤（噪音产生的误识别绝大多数是单个字）
        clean = text.replace(" ", "")
        if len(clean) <= 1:
            logger.debug(f"[过滤] 单字噪音: 「{text}」")
            return

        self.last_utterance_time = time.time()

        if self.state == State.IDLE:
            self._handle_idle(text)

        elif self.state == State.WAKE_DETECTED:
            self._handle_wake_detected(text)

        elif self.state == State.LISTENING_CMD:
            self._handle_listening_cmd(text)

        elif self.state == State.FOLLOWUP:
            self._handle_followup(text)

    def _handle_idle(self, text: str):
        """空闲状态：ASR 文本匹配唤醒词（后备方案）"""
        matched, idx, match_len = self._match_wake_word(text)
        if not matched:
            return

        now = time.time()
        if now - self.last_wake_time < config.WAKE_COOLDOWN:
            logger.info(f"唤醒冷却中")
            return

        self.last_wake_time = now

        # 提取唤醒词后面的文本作为命令
        cmd = text[idx + match_len:].strip()
        for ch in "，。！？、；：,.\n":
            cmd = cmd.replace(ch, "")

        if cmd:
            logger.info(">>> 唤醒词检测成功（一句话模式）")
            self._submit_command(cmd)
        else:
            self.state = State.WAKE_DETECTED
            self.cmd_buffer = []
            logger.info(f">>> 唤醒词「{config.WAKE_WORD}」检测成功！请说出指令...")

    def _handle_wake_detected(self, text: str):
        """唤醒后等待命令：收到第一段语音，开始录制"""
        # 跳过只有唤醒词本身的语音段（KWS 和 VAD 截取的同一句话）
        cleaned = text
        for ch in "，。！？、；：,.\n ":
            cleaned = cleaned.replace(ch, "")
        if cleaned == config.WAKE_WORD or cleaned == config.WAKE_WORD.replace("小", ""):
            logger.debug(f"[跳过] 仅唤醒词「{text}」")
            self.last_utterance_time = time.time()
            return

            logger.debug(f"[命令片段] {text}")
        self.state = State.LISTENING_CMD
        self.cmd_buffer = [text]

    def _handle_listening_cmd(self, text: str):
        """命令录制中：追加文本"""
        logger.debug(f"[命令片段] {text}")
        self.cmd_buffer.append(text)

    def _handle_followup(self, text: str):
        """后续侦听中收到语音 → 直接作为命令提交（无需唤醒）"""
        # 跳过唤醒词本身（防止误触发）
        cleaned = text
        for ch in "，。！？、；：,.\n ":
            cleaned = cleaned.replace(ch, "")
        matched, _, _ = self._match_wake_word(cleaned)
        if matched:
            logger.debug(f"[后续] 收到唤醒词，重置")
            self.last_utterance_time = time.time()
            return

        logger.debug(f"[后续追问] {text}")
        self.cmd_buffer = [text]
        self.state = State.LISTENING_CMD

    def _finish_command(self):
        """命令录制结束，拼接成一条，发送或缓冲"""
        cmd = " ".join(self.cmd_buffer)
        self.cmd_buffer = []
        if not cmd.strip():
            self._enter_followup()
            return

        if self._waiting_response:
            # 上一条还在等回复，缓冲等回复到了再发
            logger.debug(f"[缓冲] 等回复中: {cmd[:50]}")
            self._pending_cmd = cmd
            self._enter_followup()
            return

        self._submit_command(cmd)

    def _match_wake_word(self, text: str):
        """匹配唤醒词，返回 (matched, match_pos, match_len)"""
        if config.WAKE_WORD in text:
            idx = text.find(config.WAKE_WORD)
            return True, idx, len(config.WAKE_WORD)
        core = config.WAKE_WORD.replace("小", "")
        if len(core) >= 2 and core in text:
            idx = text.find(core)
            return True, idx, len(core)
        return False, -1, 0

    def _submit_command(self, text: str):
        """提交命令到 OpenClaw（异步），然后进入后续侦听等待回复"""
        text = text.strip()
        if not text:
            self._goto_idle()
            return

        logger.debug(f"[命令] {text}")
        logger.debug("提交到 OpenClaw...")
        self.client.send_text(text)
        self._waiting_response = True
        # 进入后续侦听：用户可以不等回复直接继续说话
        self._enter_followup()

    def _goto_idle(self):
        """回到空闲状态"""
        self.state = State.IDLE
        self.cmd_buffer = []
        self._waiting_response = False
        self._pending_cmd = ""

    # ─── TTS 播报 ──────────────────────────────────────────────

    def _speak(self, text: str):
        """暂停麦克风 → TTS 播报 → 恢复麦克风。"""
        if not _tts:
            return
        logger.debug("状态: TTS 播报中...")
        self.state = State.PLAYING
        self._pause_mic()
        try:
            _tts.speak(text)
        except Exception as e:
            logger.error(f"TTS 播报异常: {e}")
        self._resume_mic()

    # ─── Agent 响应处理（TTS 播报）──────────────────────────

    def _on_agent_response(self, text: str):
        """Agent 响应回调（在 CLI 线程中执行），将事件放入策略队列。"""
        self.event_queue.put(AgentResponseEvent(text=text))

    def _handle_agent_response(self, event: AgentResponseEvent):
        """策略线程中处理 Agent 响应：TTS 播报，然后处理缓冲/后续侦听。"""
        self._waiting_response = False
        logger.debug(f"收到 Agent 响应: {event.text[:80]}...")

        if self._pending_cmd:
            # 有缓冲命令：先播报回复，再发送缓冲
            logger.info(f"[发送缓冲] {self._pending_cmd[:50]}")
            cmd = self._pending_cmd
            self._pending_cmd = ""
            if _tts:
                self._speak(event.text)
            self._submit_command(cmd)
            return

        # 无缓冲：播报 → 后续侦听
        if _tts:
            self._speak(event.text)
        self._enter_followup()

    def _enter_followup(self):
        """进入多轮后续侦听状态"""
        self.state = State.FOLLOWUP
        self.last_utterance_time = time.time()
        self.cmd_buffer = []
        logger.debug(f"状态: 后续侦听中（{config.FOLLOWUP_TIMEOUT}s 超时）...")

    def _pause_mic(self):
        """暂停麦克风采集。"""
        if self.mic_stream and self.mic_stream.active:
            try:
                self.mic_stream.stop()
                logger.debug("麦克风已暂停")
            except Exception as e:
                logger.warning(f"暂停麦克风失败: {e}")

    def _resume_mic(self):
        """恢复麦克风采集。"""
        if self.mic_stream and not self.mic_stream.active:
            try:
                self.mic_stream.start()
                logger.debug("麦克风已恢复")
            except Exception as e:
                logger.warning(f"恢复麦克风失败: {e}")

    # ─── 主控制 ──────────────────────────────────────────────

    def run(self):
        """启动主循环（打开麦克风，阻塞直到退出）"""
        self.vad = VADProcessor(
            aggressiveness=config.VAD_AGGRESSIVENESS,
            speech_threshold=config.VAD_SPEECH_FRAMES,
            silence_threshold=config.VAD_SILENCE_FRAMES,
            pre_buffer_frames=config.VAD_PRE_BUFFER_FRAMES,
        )

        try:
            self.mic_stream = sd.InputStream(
                samplerate=config.SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=config.CHUNK_SIZE,
                callback=self._audio_callback,
            )
            with self.mic_stream:
                logger.debug("麦克风已打开，按 Ctrl+C 退出")
                while self.running:
                    time.sleep(0.1)
        except Exception as e:
            logger.error(f"音频流错误: {e}")
            sys.exit(1)
        finally:
            logger.info("OpenClaw Bridge 已停止")

    def stop(self):
        self.running = False


def main():
    parser = argparse.ArgumentParser(description="OpenClaw Bridge")
    parser.add_argument("--model-dir", type=str, default=config.MODEL_DIR)
    parser.add_argument("--openclaw-url", type=str, default=config.OPENCLAW_URL)
    parser.add_argument("--no-kws", action="store_true", help="禁用 KWS，仅用 ASR 匹配唤醒词")
    parser.add_argument("--debug", action="store_true", help="开启 DEBUG 日志（默认只显示 INFO）")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().handlers[0].setLevel(logging.DEBUG)
    if args.model_dir:
        config.MODEL_DIR = args.model_dir
    if args.openclaw_url:
        config.OPENCLAW_URL = args.openclaw_url
    if args.no_kws:
        config.KWS_ENABLED = False

    bridge = VoiceBridge()

    def signal_handler(sig, frame):
        logger.info("收到退出信号...")
        bridge.stop()

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    bridge.run()


if __name__ == "__main__":
    main()
