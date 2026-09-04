# J.A.R.V.I.S. 🤖

### Just A Rather Very Intelligent System

J.A.R.V.I.S. is a **local-first AI assistant** built to help with software development, studying, hackathons, productivity, system interaction, voice assistance, vision, and project management.

It combines **local Large Language Models, voice recognition, text-to-speech, computer vision, developer tools, Git integration, memory, workflows, and a futuristic web interface** into one unified AI assistant.

---

## 🚀 Features

### 🧠 AI Chat & Context

- Natural-language conversations
- Context-aware responses
- Conversation history and memory
- Intent-based request routing
- Follow-up understanding such as "this project", "this PDF", and "its status"

### 🤖 Multi-Model AI

J.A.R.V.I.S. automatically uses different local models depending on the task:

| Purpose | Model |
|---|---|
| General AI & conversation | `llama3.1:8b` |
| Coding & debugging | `qwen2.5-coder:7b` |
| Vision & image analysis | `llama3.2-vision:latest` |

All models run through **Ollama**.

### 💻 Developer Assistant

J.A.R.V.I.S. provides tools for:

- Code analysis
- Project analysis
- Debugging
- Git status and repository information
- Development environment checks
- File operations
- Terminal interaction
- Project health analysis
- Screen and OCR analysis

### 🎙️ Voice Assistant

Real-time voice interaction is supported using:

- **LiveKit** — real-time audio communication
- **Faster-Whisper** — speech-to-text
- **Piper TTS** — text-to-speech

Voice states include:

`IDLE → LISTENING → THINKING → EXECUTING → VERIFYING → SPEAKING`

### 👁️ Vision

Using `llama3.2-vision`, J.A.R.V.I.S. can analyze:

- Images
- Screenshots
- Visual errors
- Diagrams
- Uploaded visual content

### 📄 Document Analysis

J.A.R.V.I.S. can process documents such as PDFs and answer questions based on their contents.

Example:

```text
Upload PDF
    ↓
Analyze document
    ↓
Ask questions
    ↓
Get contextual answers
````

### 🛡️ Safe System Actions

System actions use permission levels:

* **SAFE** — can execute directly
* **CONFIRM** — requires user confirmation
* **BLOCKED** — cannot be executed

J.A.R.V.I.S. is designed to verify actions instead of simply assuming that an operation succeeded.

### 🧩 Specialized Assistants

The project includes specialized assistants for:

* 👨‍💻 Development
* 🎓 CSE / Computer Science
* 📚 Study
* 🏆 Hackathons

### 🔄 Workflows

Built-in workflows include:

* Project Review
* Development Environment Preparation
* Exam Preparation
* Study Sessions
* Hackathon Assistance

### 🎨 Futuristic Interface

The frontend includes:

* Arc Reactor-inspired UI
* Voice activation
* Live conversation
* File uploads
* Image preview
* Markdown responses
* Code highlighting
* Assistant state indicators

---

## 🏗️ Architecture

```text
User
 │
 ▼
Frontend
 │
 ▼
FastAPI Backend
 │
 ├── Intent Router
 ├── Context Manager
 ├── Tool Registry
 └── LLM Orchestrator
          │
          ├── General Model
          ├── Coding Model
          └── Vision Model
                  │
                  ▼
                Ollama
```

### Voice Pipeline

```text
Microphone
    ↓
LiveKit
    ↓
Faster-Whisper
    ↓
Intent + Context
    ↓
LLM / Tools
    ↓
Piper TTS
    ↓
LiveKit
    ↓
Speaker
```

---

## 🛠️ Technology Stack

| Category          | Technology            |
| ----------------- | --------------------- |
| Backend           | Python                |
| API               | FastAPI               |
| AI Runtime        | Ollama                |
| General Model     | Llama 3.1 8B          |
| Coding Model      | Qwen 2.5 Coder 7B     |
| Vision Model      | Llama 3.2 Vision      |
| Speech-to-Text    | Faster-Whisper        |
| Text-to-Speech    | Piper                 |
| Voice             | LiveKit               |
| Frontend          | HTML, CSS, JavaScript |
| Markdown          | Marked                |
| Code Highlighting | Highlight.js          |
| OCR               | Tesseract             |
| System Monitoring | psutil                |
| Memory            | Mem0 / Project Memory |

---

## 📁 Project Structure

```text
J.A.R.V.I.S/
│
├── main.py
├── config.py
├── intent_router.py
├── context_manager.py
├── workflow_engine.py
├── tool_registry.py
├── tools.py
├── terminal_tools.py
├── screen_tools.py
├── code_analysis.py
├── git_tools.py
├── file_ops.py
├── permissions.py
├── project_health.py
├── memory.py
├── project_memory.py
├── agent.py
├── whisper_stt.py
├── piper_tts.py
│
├── core/
│   └── llm_orchestrator.py
│
├── assistants/
│   ├── cse_assistant.py
│   ├── developer_assistant.py
│   ├── hackathon_assistant.py
│   └── study_assistant.py
│
├── workflows/
│   ├── project_review.py
│   ├── dev_env_prep.py
│   ├── exam_prep.py
│   ├── hackathon.py
│   └── study_session.py
│
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js
│
├── requirements.txt
├── startup.bat
└── .gitignore
```

---

## ⚙️ Installation

### 1. Clone the repository

```powershell
git clone https://github.com/mithileashvs/J.A.R.V.I.S.git
cd J.A.R.V.I.S
```

### 2. Create virtual environment

```powershell
python -m venv venv
```

Activate it:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```powershell
pip install -r requirements.txt
```

### 4. Install Ollama models

```powershell
ollama pull llama3.1:8b
ollama pull qwen2.5-coder:7b
ollama pull llama3.2-vision:latest
```

Check installed models:

```powershell
ollama list
```

### 5. Configure LiveKit

For local development:

```env
LIVEKIT_URL=ws://localhost:7880
LIVEKIT_API_KEY=devkey
LIVEKIT_API_SECRET=secret
```

Start LiveKit:

```powershell
C:\livekit\livekit-server.exe --dev
```

### 6. Start J.A.R.V.I.S.

Start the backend:

```powershell
python main.py
```

Start the frontend:

```powershell
cd frontend
python -m http.server 3000
```

Open:

```text
http://localhost:3000
```

If configured, the project can also be started using:

```powershell
.\startup.bat
```

---

## 🔐 Security

J.A.R.V.I.S. follows a **local-first architecture**.

Never commit:

* `.env` files
* API keys
* Passwords
* Access tokens
* Private credentials
* Model weights
* Databases
* Logs
* Virtual environments

The `.gitignore` file is configured to exclude common secrets, caches, databases, model files, logs, and generated files.

---

## 🧭 Design Principles

### Local First

Prefer local AI models and services whenever practical.

### Intent First

Understand the user's request before selecting a model or tool.

### Context Aware

Maintain awareness of the current project, files, documents, applications, and recent actions.

### Model Specialization

Use the appropriate AI model for general conversation, coding, or vision.

### Safe Execution

Sensitive operations require confirmation or are blocked.

### Verified Actions

Actions should be verified before being reported as successful.

### Modular Architecture

Assistants, tools, workflows, memory, and model routing are separated into maintainable modules.

---

## 🧪 Development Checks

Check Python syntax:

```powershell
python -m compileall -q .
```

Check frontend JavaScript:

```powershell
node --check frontend/app.js
```

Check Git:

```powershell
git status
```

---

## 🗺️ Future Development

Planned areas of improvement include:

* Improved voice and text pipeline integration
* Better context resolution
* Stronger action verification
* Advanced project intelligence
* Expanded developer automation
* Improved document and vision workflows
* More proactive assistance
* Enhanced memory
* Additional local AI models
* More comprehensive testing
* Expanded system interaction

---

## 🌟 Vision

The goal of J.A.R.V.I.S. is to build a **personal AI operating layer** that can understand the user's environment, reason about tasks, use appropriate tools, and assist with development, learning, productivity, and computer interaction.

```text
Understand
    ↓
Reason
    ↓
Select Model / Tool
    ↓
Execute
    ↓
Verify
    ↓
Respond
```

> **A personal AI assistant that runs on your machine, understands your context, and helps you get things done.**

---

## 👥 Contributors

Developed as the **J.A.R.V.I.S. project**.

Contributions, improvements, issues, and feature ideas are welcome.

---

## 📜 License

Add the project's chosen license here when the project is formally released as open source.

````

Then save it as:

```text
README.md
````

in:

```text
C:\Users\USER\Desktop\J.A.R.V.I.S
```

and run:

```powershell
git add README.md
git commit -m "docs: add comprehensive README"
git push
```
