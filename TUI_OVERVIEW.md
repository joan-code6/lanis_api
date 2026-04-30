# 🎓 Schulportal Hessen TUI - Complete Overview

I've built a **production-grade Terminal User Interface** for your Schulportal Hessen API using **Ink** (React for terminals). Here's what you now have:

## 📦 What Was Created

### 1. **TUI Application** (`/tui` directory)

A complete, type-safe React application with:

- **Components**: Login, Dashboard, Messages, Calendar, Courses, Profile
- **API Client**: Type-safe Axios wrapper with full error handling
- **Design System**: Cohesive theme with colors, icons, and patterns
- **TypeScript**: Full type safety for development

### 2. **Complete Documentation**

- **TUI_QUICKSTART.md** - Get running in 30 seconds
- **tui/README.md** - Full TUI documentation
- **tui/QUICK_REFERENCE.md** - Keyboard shortcuts & tips
- **tui/ADVANCED.md** - Development & extension guide
- **tui/DESIGN_SYSTEM.md** - Design philosophy & customization

### 3. **Setup Scripts**

- **setup.bat** - Windows automatic setup
- **setup.sh** - macOS/Linux automatic setup
- Both check for dependencies and install everything

### 4. **Theme & Utilities**

- **theme.tsx** - Color palette, icons, components
- **Layout.tsx** - Reusable UI components (Card, Stack, etc.)
- **LoadingSpinner.tsx** - Animated loading indicator

## 🎨 Design Philosophy

The TUI uses a **refined minimalism** aesthetic:

✨ **Strategic color usage** - Cyan (primary), Green (success), Yellow (loading), Red (error)
✨ **Clear hierarchy** - Bold titles → regular text → dimmed hints
✨ **Unicode symbols** - 📨 Messages, 📅 Calendar, 🎓 Courses, etc.
✨ **Consistent spacing** - Predictable padding and margins
✨ **Accessibility first** - Keyboard-only navigation, high contrast

## 📁 Directory Structure

```
lanis_api/
│
├── tui/                           ← NEW: Terminal UI
│   ├── src/
│   │   ├── index.tsx              # CLI entry point
│   │   ├── App.tsx                # Main app component
│   │   ├── api.ts                 # API client
│   │   ├── types.ts               # TypeScript types
│   │   ├── theme.tsx              # Design system
│   │   ├── components/
│   │   │   ├── LoginScreen.tsx
│   │   │   ├── Dashboard.tsx
│   │   │   ├── MessagesView.tsx
│   │   │   ├── CalendarView.tsx
│   │   │   ├── CoursesView.tsx
│   │   │   ├── ProfileView.tsx
│   │   │   ├── Layout.tsx
│   │   │   ├── LoadingSpinner.tsx
│   │   │   └── bin.tsx
│   │
│   ├── package.json               # Node.js dependencies
│   ├── tsconfig.json              # TypeScript config
│   ├── README.md                  # TUI documentation
│   ├── QUICK_REFERENCE.md         # Keyboard shortcuts
│   ├── ADVANCED.md                # Development guide
│   ├── DESIGN_SYSTEM.md           # Design documentation
│   ├── .env.example               # Configuration template
│   └── .gitignore
│
├── setup.bat                      ← NEW: Windows setup
├── setup.sh                       ← NEW: Unix setup
├── TUI_QUICKSTART.md              ← NEW: Quick start guide
├── README_TUI.md                  ← NEW: Complete overview
│
├── api/                           # Existing: FastAPI backend
├── functions/                     # Existing: Core logic
└── [other existing files]
```

## 🚀 Quick Start

### 1. Automatic Setup (Easiest)

**Windows:**
```bash
setup.bat
```

**macOS/Linux:**
```bash
chmod +x setup.sh
./setup.sh
```

### 2. Manual Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Install Node.js dependencies
cd tui
npm install
npm run build
cd ..
```

### 3. Run

**Terminal 1:**
```bash
python -m uvicorn api:app --reload
```

**Terminal 2:**
```bash
cd tui && npm run dev
```

## 🎮 Features

### Login Screen
- Three-step form (School ID → Username → Password)
- Secure input with masked password
- Error handling and retry

### Dashboard
- Clean menu-based navigation
- Status indicators (unread messages, upcoming events)
- Quick access to all features

### Messages (📨)
- View all conversations
- Organized by sender
- Quick-scan interface

### Calendar (📅)
- Upcoming events chronologically
- Date and time information
- Visual organization

### Courses (🎓)
- All enrolled classes
- Instructor information
- Quick overview

### Profile (👤)
- Account details
- Session information
- Encryption status

## ⌨️ Navigation

```
↑ / ↓      Navigate items
Enter      Select / Confirm
Esc        Back / Cancel
Ctrl+C     Exit
```

## 🔧 Configuration

### API Endpoint

```bash
# Development (default)
API_URL=http://localhost:8000 npm run dev

# Production
API_URL=https://api.example.com npm run dev
```

### Debug Mode

Create `tui/.env`:
```env
API_URL=http://localhost:8000
DEBUG=true
```

## 📊 Technology Stack

### Frontend (TUI)
- **React 18** - Component framework
- **Ink** - Terminal React renderer
- **TypeScript** - Type safety
- **Axios** - HTTP client
- **ink-select-input** - Menu components
- **ink-text-input** - Form inputs

### Backend (Existing)
- **FastAPI** - Web framework
- **Python 3.8+** - Runtime
- **Requests** - HTTP client

## 🛠 Development

### Build
```bash
cd tui
npm run build
```

### Type Checking
```bash
npm run type-check
```

### Development Server
```bash
npm run dev
```

### Add New Feature
See `tui/ADVANCED.md` for detailed guide on:
- Adding new views
- Extending API client
- Custom styling
- State management
- Advanced patterns

## 🎨 Customization

### Colors
Edit `tui/src/theme.tsx`:
```typescript
export const colors = {
  primary: 'magenta',  // Change primary color
  success: 'green',
  // ...
};
```

### Styling
All components use:
- `Box` for layout (flex, padding, borders)
- `Text` for typography (bold, color, dims)
- Named colors from theme
- Consistent spacing rules

### New Components
Create in `tui/src/components/`:
```typescript
export const MyComponent = () => (
  <Box borderStyle="round" borderColor="cyan">
    <Text bold color="cyan">Title</Text>
  </Box>
);
```

## 📚 Documentation Files

| File | Purpose |
|------|---------|
| `TUI_QUICKSTART.md` | Get started in 30 seconds |
| `README_TUI.md` | Complete project overview |
| `tui/README.md` | TUI-specific documentation |
| `tui/QUICK_REFERENCE.md` | Keyboard shortcuts & workflows |
| `tui/ADVANCED.md` | Development & extension guide |
| `tui/DESIGN_SYSTEM.md` | Design philosophy & customization |

## 🔐 Security

✓ Token-based sessions (not stored)
✓ Passwords never saved
✓ Automatic session cleanup
✓ HTTPS-ready
✓ No plain-text credentials

## 📈 Performance

- Cached responses (10-minute TTL)
- Efficient state management
- Minimal re-renders
- Quick navigation between views
- Non-blocking API calls

## ⚡ Performance Optimization

- Components are memoized
- API responses cached smartly
- State persists during navigation
- Lazy loading for profiles

## 🐛 Troubleshooting

| Issue | Solution |
|-------|----------|
| "Connection refused" | Start API: `python -m uvicorn api:app --reload` |
| "Module not found" | Run `npm install` in tui directory |
| "Invalid credentials" | Check username (no @ symbol needed) |
| Terminal garbled | Use modern terminal (Windows Terminal, iTerm2, etc.) |

See `tui/QUICK_REFERENCE.md` for more troubleshooting.

## 🚀 Next Steps

1. **Get it running:**
   ```bash
   setup.bat        # Windows
   ./setup.sh       # macOS/Linux
   ```

2. **Read the guide:**
   - Start: `TUI_QUICKSTART.md`
   - Reference: `tui/QUICK_REFERENCE.md`
   - Develop: `tui/ADVANCED.md`

3. **Customize:**
   - Colors: `tui/src/theme.tsx`
   - Layout: `tui/src/components/`
   - Features: `tui/ADVANCED.md`

4. **Deploy:**
   ```bash
   npm run build     # Build TypeScript
   npx pkg dist/cli.js --output lanis  # Create binary
   ```

## 📝 Examples

### Add a new menu item
1. Add to `Dashboard` component
2. Handle in `App.tsx` switch statement
3. Create corresponding view component

### Fetch new data
1. Add method to `api.ts`
2. Call from `App.tsx`
3. Pass to component
4. Display

### Change theme colors
1. Edit `tui/src/theme.tsx`
2. Run `npm run build`
3. Done!

## 🎓 Learning Resources

- Ink GitHub: https://github.com/vadimdemedes/ink
- React Docs: https://react.dev
- TypeScript: https://www.typescriptlang.org
- Terminal Design: See `tui/DESIGN_SYSTEM.md`

## 💡 Tips & Tricks

- **Keyboard only**: The entire UI is keyboard-driven
- **No mouse needed**: Full accessibility
- **Fast navigation**: State persists when switching screens
- **Terminal size**: Works best at 60+ characters wide
- **Dark mode**: Automatically adapts to your terminal theme

## 🎉 What You Can Do Now

✅ Authenticate securely
✅ View all messages/conversations
✅ Check calendar and deadlines
✅ Browse courses and instructors
✅ See profile information
✅ Navigate with keyboard
✅ Extend with new features
✅ Customize colors & styling
✅ Deploy as executable
✅ Share with others

## 📞 Support

- **Documentation**: See files listed above
- **Issues**: Check troubleshooting guide
- **Development**: See `tui/ADVANCED.md`
- **Questions**: Check `tui/QUICK_REFERENCE.md`

---

**You now have a production-grade Terminal UI for Schulportal Hessen!** 🎓✨

Start with `TUI_QUICKSTART.md` and enjoy! 🚀
