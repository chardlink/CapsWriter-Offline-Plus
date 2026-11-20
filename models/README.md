# 语音识别模型目录 (`models/`)

为了保持代码仓库轻量化，语音识别与标点模型文件不包含在 Git 仓库中，请单独下载后解压放入本目录。

## 下载地址

- **本仓库独立模型备份**：<https://github.com/chardlink/CapsWriter-Offline-Plus/releases/tag/models>
- **原项目模型发布页（备用）**：<https://github.com/HaujetZhao/CapsWriter-Offline/releases/tag/models>

## 目录结构示例

按 `config_server.py` 中的 `model_type` 配置，将对应的模型放置到以下子目录中：

- `models/Qwen3-ASR/Qwen3-ASR-1.7B/`（当前默认 `model_type = 'qwen_asr'`）
  - `qwen3_asr_encoder_frontend.onnx`
  - `qwen3_asr_encoder_backend.onnx`
  - `qwen3_asr_llm.gguf`
- `models/Qwen3-ForcedAligner/Qwen3-ForcedAligner-0.6B/`（可选对齐模型）
- `models/Fun-ASR-Nano/Fun-ASR-Nano-GGUF/`
- `models/SenseVoice-Small/Sensevoice-Small-ONNX/`
- `models/Paraformer/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-onnx/`
- `models/Punct-CT-Transformer/sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12/`
