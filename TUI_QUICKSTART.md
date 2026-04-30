# Getting Started with the Schulportal Hessen CLI TUI

## Quick Start

### 1. Start the API Server

```bash
# In the main project directory
uvicorn api:app --reload
```

The API will be available at `http://localhost:8000`

### 2. Install TUI Dependencies

```bash
# Navigate to the tui directory
cd tui

# Install dependencies
npm install
```

### 3. Run the TUI

```bash
# Start development mode
npm run dev

# Or after building
npm run build
npm start
```

## Features Overview

### Login Screen
- Enter your School ID (Schul-ID)
- Enter your username
- Enter your password securely
- Sessions are managed automatically

### Dashboard
- Quick access to all major features
- Shows notification counts (unread messages, upcoming events)
- Clean menu-driven navigation

### Messages (📨)
- View all your messages
- Browse conversations
- See message threads organized by sender

### Calendar (📅)
- Check upcoming events and deadlines
- View event dates and times
- Stay organized with your school schedule

### My Classes (🎓)
- Browse your courses and classes
- See instructor information
- View course details

### Profile (👤)
- View your account information
- Check encryption status
- Session details

## Architecture

```
┌─────────────────────────────────────────────┐
│         Schulportal Hessen CLI TUI          │
│             (Ink + React)                   │
├─────────────────────────────────────────────┤
│                                             │
│  Components:                                │
│  • LoginScreen                              │
│  • Dashboard                                │
│  • MessagesView                             │
│  • CalendarView                             │
│  • CoursesView                              │
│  • ProfileView                              │
│                                             │
└────────────────────┬────────────────────────┘
                     │ (HTTP)
┌────────────────────▼────────────────────────┐
│        FastAPI Backend                      │
│   (Session Management & Routing)            │
├─────────────────────────────────────────────┤
│                                             │
│  • /login - Authentication                  │
│  • /nachrichten - Messages                  │
│  • /kalender - Calendar                     │
│  • /mein-unterricht - Courses               │
│  • /benutzer - Profile                      │
│                                             │
└────────────────────┬────────────────────────┘
                     │
┌────────────────────▼────────────────────────┐
│  SchulportalHessen API Client               │
│  (Web Scraping & Data Parsing)              │
├─────────────────────────────────────────────┤
│                                             │
│  • Session Management                       │
│  • HTML Parsing                             │
│  • Data Extraction                          │
│                                             │
└────────────────────┬────────────────────────┘
                     │ (HTTPS)
                     │
         ┌───────────▼────────────┐
         │  schulportal.hessen.de │
         │  (Official Portal)     │
         └───────────────────────┘
```

## Configuration

### Environment Variables

Create a `.env` file in the `tui` directory:

```env
# API endpoint (defaults to http://localhost:8000)
API_URL=http://localhost:8000

# Optional debug logging
DEBUG=false
```

## Development

### Build

```bash
npm run build
```

Output files will be in the `dist/` directory.

### Type Checking

```bash
npm run type-check
```

### Project Structure

```
tui/
├── src/
│   ├── index.tsx              # CLI entry point
│   ├── cli.tsx                # Styled CLI wrapper
│   ├── App.tsx                # Main app component
│   ├── api.ts                 # API client
│   ├── types.ts               # TypeScript types
│   └── components/
│       ├── LoginScreen.tsx
│       ├── Dashboard.tsx
│       ├── MessagesView.tsx
│       ├── CalendarView.tsx
│       ├── CoursesView.tsx
│       └── ProfileView.tsx
├── package.json
├── tsconfig.json
├── README.md
└── .env.example
```

## Troubleshooting

### API Connection Errors

Make sure the API server is running on the correct port:

```bash
# Check if API is running
curl http://localhost:8000/docs

# Change API URL if needed
API_URL=http://your-server:8000 npm run dev
```

### Login Issues

- Verify your credentials are correct
- Ensure your school ID is in the correct format (usually numeric)
- Check that encryption is enabled in your session

### Terminal Compatibility

Works best with:
- Windows Terminal (Windows 10+)
- iTerm 2 / Terminal.app (macOS)
- GNOME Terminal / Konsole (Linux)

## Tips & Tricks

1. **Quick Navigation**: Use arrow keys to navigate, Enter to select, Esc to go back
2. **Keyboard Only**: The TUI is fully keyboard-driven - no mouse required
3. **Fast Switching**: Navigate between different sections without losing state
4. **Terminal Size**: For best experience, use a terminal window at least 60 characters wide

## License

MIT
