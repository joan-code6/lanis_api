# TUI Quick Reference

## 🚀 Getting Started (30 seconds)

```bash
# Terminal 1: Start API
python -m uvicorn api:app --reload

# Terminal 2: Start TUI
cd tui && npm run dev
```

## ⌨️ Keyboard Controls

```
↑ / ↓      Navigate menu items
Enter      Select / Confirm input
Esc        Go back to menu
Ctrl+C     Exit application
Tab        Next field (in forms)
```

## 🔐 Login

1. Enter **Schul-ID** (school code, usually 4 digits)
2. Enter **Benutzername** (username, often firstname.lastname)
3. Enter **Passwort** (password) - shown as dots for security
4. Press Enter to authenticate

## 📍 Main Menu

```
📨 Nachrichten     → View messages/conversations
📅 Kalender        → Check upcoming events
🎓 Mein Unterricht → Browse your courses
👤 Profil          → View account info
⚙️ Einstellungen   → Settings (future)
🚪 Abmelden        → Logout
```

## 📨 Messages View

- Shows all conversations
- Sorted by sender name
- Last 30 days by default
- Select to view details

**Pro tip**: Press Esc to return to dashboard

## 📅 Calendar View

- Chronologically sorted events
- Shows date and time
- Includes deadlines and classes
- Count of upcoming events in menu

## 🎓 Courses View

- All enrolled courses listed
- Shows instructor name
- Quick overview of schedule
- Access course materials (when available)

## 👤 Profile

- **Username**: Your login username
- **School ID**: Your school code
- **Encryption**: Status of secure connection
- **Token**: Session status

## ⚠️ Error Messages

```
✘ Login failed        → Check credentials
✘ Connection error    → Check API is running
✘ Session expired     → Log in again
⟳ Loading...          → Wait for operation
```

## 🔧 Environment Setup

### Windows

```bash
# Automatic
setup.bat

# Manual
pip install -r requirements.txt
cd tui
npm install
npm run build
```

### macOS/Linux

```bash
# Automatic
chmod +x setup.sh
./setup.sh

# Manual (same as Windows above)
```

## 🌐 API Configuration

### Change API URL

**Option 1**: Environment variable

```bash
API_URL=http://192.168.1.100:8000 npm run dev
```

**Option 2**: Edit `.env` file

```env
API_URL=http://localhost:8000
DEBUG=false
```

## 🐛 Troubleshooting

| Problem | Solution |
|---------|----------|
| "Connection refused" | API not running. Start with `python -m uvicorn api:app --reload` |
| "Invalid credentials" | Check username/password. No @ symbol needed |
| "Command not found: npm" | Install Node.js from https://nodejs.org |
| "ModuleNotFoundError" | Run `pip install -r requirements.txt` |
| Terminal is garbled | Use a terminal with ANSI color support (Windows Terminal, iTerm2, etc.) |

## 📊 Directory Structure

```
lanis_api/
├── api/              # FastAPI backend
├── functions/        # Core logic
├── tui/              # Terminal UI
│   ├── src/         # Source code
│   ├── dist/        # Built files
│   └── package.json
└── requirements.txt
```

## 🔄 Common Workflows

### View Unread Messages

1. From dashboard: Press ↓ to highlight **Nachrichten**
2. Press Enter
3. Messages appear (unread count in title)
4. Press Esc to return

### Check Today's Schedule

1. From dashboard: Press ↓↓ to highlight **Kalender**
2. Press Enter
3. Upcoming events listed
4. Check times and locations

### Browse Course Materials

1. From dashboard: Press ↓↓↓ to highlight **Mein Unterricht**
2. Press Enter
3. All courses listed with instructor
4. Select course for details (if available)

## 💾 Data Management

### What's Stored

- ✓ Session token (only in RAM, cleared on logout)
- ✓ Fetched data (messages, calendar, courses)
- ✗ Credentials (never stored)
- ✗ Cache files (no persistence)

### What's Not Stored

- Passwords are never saved
- Sessions expire after 1 hour of inactivity
- Log out to clear session immediately
- Browser cookies are not used

## 🎨 Customization

### Change Colors

Edit `tui/src/theme.tsx`:

```typescript
export const colors = {
  primary: 'magenta',  // Change primary color
  success: 'green',
  // ...
};
```

### Add Custom View

1. Create `tui/src/components/YourFeature.tsx`
2. Add to `tui/src/App.tsx`
3. Run `npm run build`

See `tui/ADVANCED.md` for detailed guide

## 📚 Help & Resources

- **Full TUI Guide**: `TUI_QUICKSTART.md`
- **Advanced Features**: `tui/ADVANCED.md`
- **Design System**: `tui/DESIGN_SYSTEM.md`
- **API Docs**: `api-documentation.md`
- **Interactive API**: http://localhost:8000/docs

## 🔐 Security Tips

1. ✓ Change password regularly
2. ✓ Don't share session tokens
3. ✓ Log out when finished
4. ✓ Use HTTPS when available
5. ✓ Keep system updated

## ⚡ Performance Tips

- API responses are cached for 10 minutes
- Switching screens doesn't reload data
- Sessions remain active for 1 hour
- Data persists until logout

## 🚀 Advanced

### Build Production Binary

```bash
npm run build
npx pkg dist/index.js --output lanis-cli
```

### Docker Setup

```bash
docker build -t lanis-tui .
docker run -e API_URL=http://host.docker.internal:8000 lanis-tui
```

### CI/CD Integration

```bash
npm run type-check  # Verify TypeScript
npm run build       # Build
npm test            # Run tests
```

## 📞 Getting Help

1. Check this cheat sheet
2. Read the detailed guides
3. Check API health: `curl http://localhost:8000/health`
4. View logs: Check terminal output
5. Open an issue on GitHub

---

**Remember**: You can always press `Esc` to go back or `Ctrl+C` to exit! 🎓
