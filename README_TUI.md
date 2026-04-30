# Schulportal Hessen API + TUI

A modern, production-grade Terminal User Interface for the unofficial Schulportal Hessen API. Built with **Ink** (React for CLI) and **FastAPI**.

## 🎨 Features

### 🔐 Authentication
- Secure token-based session management
- Support for multiple concurrent users
- Encrypted data handling
- Automatic session cleanup on expiration

### 📱 User Interface
- **Refined, minimalist design** with strategic use of color
- **Keyboard-driven navigation** - fully accessible without mouse
- **Smooth state management** - navigate without losing data
- **Real-time status indicators** - loading states, error messages
- **Beautiful typography** - carefully chosen colors and text styles

### 📨 Messages
- View all conversations
- Browse message threads
- Organized by sender
- Quick-scan interface

### 📅 Calendar
- Check upcoming events
- View event details
- See deadlines and important dates
- Organized chronologically

### 🎓 Courses
- Browse your classes
- View instructor information
- See course details
- Quick overview of all courses

### 👤 Profile
- View account information
- Check encryption status
- Session details
- User credentials summary

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- Node.js 16+
- npm or yarn

### Installation

**Option 1: Automatic Setup (Recommended)**

Windows:
```bash
setup.bat
```

macOS/Linux:
```bash
chmod +x setup.sh
./setup.sh
```

**Option 2: Manual Setup**

```bash
# Install Python dependencies
pip install -r requirements.txt

# Install Node.js dependencies
cd tui
npm install
npm run build
cd ..
```

### Running

**Terminal 1 - Start the API**
```bash
python -m uvicorn api:app --reload
```

**Terminal 2 - Start the TUI**
```bash
cd tui
npm run dev
```

Then:
1. Enter your school ID (Schul-ID)
2. Enter your username
3. Enter your password
4. Navigate using arrow keys and Enter
5. Press Esc to go back

## 📁 Project Structure

```
lanis_api/
├── api/                          # FastAPI backend
│   ├── api.py                   # FastAPI routes
│   ├── metrics/                 # User metrics
│   └── queue/                   # Task queue
│
├── functions/                   # Core API logic
│   ├── base.py                 # SchulportalHessenAPI class
│   ├── applets/                # Feature modules
│   │   ├── benutzer/          # User profile
│   │   ├── kalender/          # Calendar
│   │   ├── nachrichten/       # Messages
│   │   ├── mein_unterricht/   # Courses
│   │   └── ...
│   └── tools/                 # Utilities
│
├── tui/                        # Terminal User Interface
│   ├── src/
│   │   ├── App.tsx            # Main component
│   │   ├── api.ts             # API client
│   │   ├── theme.tsx          # Design system
│   │   ├── types.ts           # TypeScript types
│   │   └── components/
│   │       ├── LoginScreen.tsx
│   │       ├── Dashboard.tsx
│   │       ├── MessagesView.tsx
│   │       ├── CalendarView.tsx
│   │       ├── CoursesView.tsx
│   │       ├── ProfileView.tsx
│   │       ├── Layout.tsx
│   │       └── LoadingSpinner.tsx
│   └── package.json
│
├── requirements.txt            # Python dependencies
├── TUI_QUICKSTART.md          # TUI setup guide
├── api-documentation.md        # API docs
└── README.md                  # This file
```

## 🔧 Configuration

### API Configuration

Set environment variables:
```bash
# API host and port (default: localhost:8000)
API_HOST=localhost
API_PORT=8000
```

Run with custom settings:
```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

### TUI Configuration

Create `tui/.env`:
```env
# API endpoint
API_URL=http://localhost:8000

# Debug mode (shows extra logging)
DEBUG=false
```

## 📚 Architecture

```
┌──────────────────────────────────────┐
│   Schulportal Hessen CLI (Ink/React) │
│   Terminal User Interface            │
├──────────────────────────────────────┤
│  Components: Login, Dashboard,       │
│  Messages, Calendar, Courses, etc.   │
└────────────┬─────────────────────────┘
             │ HTTP/REST
┌────────────▼─────────────────────────┐
│      FastAPI Backend                 │
│   Session & Route Management         │
├──────────────────────────────────────┤
│  Auth, Middleware, Response Cache    │
└────────────┬─────────────────────────┘
             │
┌────────────▼─────────────────────────┐
│  SchulportalHessenAPI Client         │
│  Web Scraping & Data Extraction      │
├──────────────────────────────────────┤
│  HTML Parsing, Session Management    │
└────────────┬─────────────────────────┘
             │ HTTPS
             ▼
    schulportal.hessen.de
```

## ⌨️ Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `↑` / `↓` | Navigate menu items |
| `Enter` | Select / Confirm |
| `Esc` | Go back / Cancel |
| `Ctrl+C` | Force quit |

## 🔐 Security

- **Token-based authentication** - Secure session tokens
- **No credential storage** - Credentials are never stored
- **Automatic session cleanup** - Sessions expire after inactivity
- **HTTPS support** - Encrypted communication with server
- **Encryption ready** - Supports encrypted data transmission

## 📊 Performance

- **Caching** - Smart caching of responses
- **Session management** - Memory-efficient session handling
- **Async operations** - Non-blocking API calls
- **Optimized rendering** - Efficient terminal updates

## 🛠 Development

### Install dev dependencies
```bash
cd tui
npm install
```

### Run development server
```bash
npm run dev
```

### Build for production
```bash
npm run build
```

### Type checking
```bash
npm run type-check
```

### Testing
```bash
python -m pytest api-tests.py
```

## 📝 API Documentation

Detailed API documentation available at:
- [API Documentation](api-documentation.md) - Full endpoint reference
- `http://localhost:8000/docs` - Interactive Swagger UI
- `http://localhost:8000/redoc` - ReDoc documentation

### Available Endpoints

- `POST /login` - Authenticate user
- `POST /logout` - End session
- `GET /modules` - List available modules
- `GET /nachrichten` - Get messages
- `GET /kalender` - Get calendar events
- `GET /mein-unterricht` - Get courses
- `GET /benutzer` - Get user profile

## 🐛 Troubleshooting

### Port Already in Use
```bash
# Kill process using port 8000
# Windows:
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# macOS/Linux:
lsof -i :8000
kill -9 <PID>
```

### API Connection Error
```bash
# Verify API is running
curl http://localhost:8000/docs

# Check network connectivity
ping localhost
```

### Terminal Display Issues
- Use a terminal with ANSI color support
- Minimum terminal width: 60 characters
- Recommended: Windows Terminal, iTerm2, GNOME Terminal

## 📦 Dependencies

### Python
- `fastapi` - Web framework
- `requests` - HTTP client
- `pydantic` - Data validation
- `uvicorn` - ASGI server

### Node.js
- `ink` - Terminal React renderer
- `react` - Component framework
- `typescript` - Type safety
- `axios` - HTTP client

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📄 License

This project is MIT licensed.

## ⚠️ Disclaimer

This is an unofficial API for Schulportal Hessen. It is not affiliated with or endorsed by Hessisches Kultusministerium. Use at your own risk and comply with Schulportal Hessen's terms of service.

## 📞 Support

For issues, questions, or suggestions, please open an issue on GitHub.

---

**Happy learning! 🎓**
