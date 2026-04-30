# Schulportal Hessen CLI TUI

A beautiful, production-grade Terminal User Interface for the Schulportal Hessen API built with Ink.

## Features

- 🎨 **Refined Aesthetic** - Clean, minimalist design with strategic use of color and typography
- 🔐 **Secure Authentication** - Token-based session management
- 📨 **Messages** - View and manage your messages
- 📅 **Calendar** - Check upcoming events
- 🎓 **Courses** - Browse your courses and classes
- 👤 **Profile** - View your profile information
- ⌨️ **Keyboard Navigation** - Smooth, intuitive keyboard controls

## Installation

```bash
# Navigate to the tui directory
cd tui

# Install dependencies
npm install

# Or with yarn
yarn install
```

## Usage

### Development

```bash
npm run dev
```

### Build

```bash
npm run build
```

### Run

```bash
npm start
# or
npx lanis
```

## Configuration

Set the API endpoint via environment variable:

```bash
API_URL=http://localhost:8000 npm run dev
```

## Architecture

### Components

- **LoginScreen** - Secure credential input with step-by-step form
- **Dashboard** - Main menu with quick access to all features
- **MessagesView** - Display and manage messages
- **CalendarView** - Browse calendar events
- **CoursesView** - View course information
- **ProfileView** - User profile and session details

### API Client

The `api.ts` module provides a clean interface to all Schulportal Hessen endpoints:

```typescript
import { api } from './api';

// Login
const session = await api.login({ schoolId, username, password });

// Get data
const messages = await api.getMessages();
const events = await api.getCalendarEvents();
const courses = await api.getCourses();

// Logout
await api.logout();
```

## Keyboard Shortcuts

- `↑` / `↓` - Navigate menu items
- `Enter` - Select item / Confirm input
- `Esc` - Go back / Exit
- `Ctrl+C` - Force quit

## Tech Stack

- **React** - Component framework
- **Ink** - Terminal React renderer
- **TypeScript** - Type safety
- **Axios** - HTTP client

## License

MIT
