"""全局配置"""

# 麦克风采样率（必须与 ASR/KWS 模型匹配）
SAMPLE_RATE = 16000

# 每帧音频块大小（毫秒）
CHUNK_MS = 30

# 每帧采样点数
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_MS / 1000)  # 480

# VAD 能量灵敏度 (0-3, 3最严格，对应 RMS 阈值越高)
VAD_AGGRESSIVENESS = 2

# VAD 语音结束判定：连续多少帧静音后认为语音结束
VAD_SILENCE_FRAMES = 15  # ~450ms

# VAD 语音起始判定：连续多少帧有声后才开始录音
VAD_SPEECH_FRAMES = 5  # ~150ms

# VAD 预缓冲帧数：保留说话前的音频帧，防止丢失语音开头
VAD_PRE_BUFFER_FRAMES = 10  # ~300ms

# 唤醒词
WAKE_WORD = "小龙虾"

# 唤醒冷却时间（秒）- 唤醒后多久才能再次唤醒
WAKE_COOLDOWN = 3.0

# 命令等待超时（秒）- 唤醒后多久没收到新语音段则放弃等待命令
CMD_TIMEOUT = 15.0

# 命令结束超时（秒）- 命令录制中，多久没新语音段则认为命令结束
CMD_END_SILENCE = 4.0

# 多轮对话后续侦听（秒）- Agent 回答后保持侦听，用户可直接追问
FOLLOWUP_TIMEOUT = 8.0

# OpenClaw 网关地址
OPENCLAW_URL = "http://127.0.0.1:18789/"
OPENCLAW_TIMEOUT = 5  # 超时秒数

# OpenClaw WebSocket 认证 Token
OPENCLAW_TOKEN = "106d50240bfdb0a93666180c7acce7afc68181d5cb91c14e"

# OpenClaw 会话标识（发送消息时必须携带，需兼容 Windows 文件名）
OPENCLAW_SESSION = "openclaw-bridge"

# OpenClaw 会话 Key（决定 Agent 通道，对应前端 URL 中的 session 参数）
# 例如前端地址 /chat?session=agent%3Amain%3Amain 则填 "agent:main:main"
OPENCLAW_SESSION_KEY = "agent:main:main"

# 语音桥消息标识前缀（附加到每条消息开头，供 Agent 识别来源并切换人格）
# 设为空字符串则不附加前缀
VOICE_BRIDGE_TAG = "[bridge] "

# ASR 模型目录
MODEL_DIR = "./models/sherpa-onnx-paraformer-zh-small-2024-03-09"

# KWS 唤醒词模型目录
KWS_MODEL_DIR = "./models/sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"

# KWS 唤醒词列表
KWS_KEYWORDS = ["小龙虾"]

# KWS 触发阈值 (0.0-1.0, 越大越难触发, 默认 0.25)
KWS_KEYWORDS_THRESHOLD = 0.25

# 是否启用 KWS（启用后作为主要唤醒方式，ASR 文本匹配作为后备）
KWS_ENABLED = True

# TTS 语音播报
TTS_ENABLED = True
TTS_MODEL_DIR = "./models/vits-icefall-zh-aishell3"  # VITS 中文模型目录
TTS_TOKENS = ""       # tokens.txt 路径（留空则从 model_dir 读取）
TTS_LEXICON = ""      # lexicon.txt 路径（留空则从 model_dir 读取）
TTS_LENGTH_SCALE = 0.7  # 语速（0.5~2.0，越小越快，TODO: 运行时可调）
TTS_KEEP_ENGLISH = False  # 保留英文和数字（换多语言 TTS 模型时设为 True）
