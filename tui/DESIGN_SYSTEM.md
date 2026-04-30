# Design System & Aesthetics

This document outlines the design philosophy and visual system used in the Schulportal Hessen TUI.

## 🎨 Design Philosophy

The TUI is built on a **refined minimalism** approach:

- **Intentional over flashy** - Every visual element serves a purpose
- **Clarity first** - Information hierarchy guides the eye
- **Restraint & focus** - Limited color palette, strategic accents
- **Consistency** - Predictable patterns and behaviors
- **Accessibility** - Keyboard-only navigation, high contrast

## 📐 Color Palette

We use a curated subset of terminal colors for maximum impact with minimum complexity:

```typescript
export const colors = {
  primary: 'cyan',      // Main brand color - used for titles, headers
  success: 'green',     // Positive actions, confirmations
  warning: 'yellow',    // Cautions, pending actions
  error: 'red',         // Errors, destructive actions
  info: 'blue',         // Informational messages
  accent: 'magenta',    // Highlights, special items
} as const;
```

### Color Usage Guidelines

| Color | Use Cases |
|-------|-----------|
| **Cyan** | Primary navigation, headers, main titles, focus states |
| **Green** | Success messages, confirmations, profiles/personal data |
| **Yellow** | Loading states, pending operations, warnings |
| **Red** | Errors, deletions, failed operations |
| **Blue** | Info boxes, alternative content, secondary actions |
| **Magenta** | Calendar, events, special highlights |

### Contrast & Accessibility

- Primary colors have high contrast against black/white backgrounds
- Avoid: pure white on black (use `dimColor` or gray instead)
- Always pair colors with icons/symbols for those with color blindness
- Never rely on color alone to convey meaning

## 📝 Typography

### Font Strategy

Terminal UIs don't have custom fonts, but we use:

1. **Monospace** - The terminal's default monospace font (usually Courier New, Monaco, or Menlo)
2. **Bold for emphasis** - Used for headers, labels, important information
3. **Regular for body** - Standard text, descriptions
4. **Dimmed for hints** - Helper text, keyboard shortcuts, secondary info

### Hierarchy

```
╔═══════════════════════════════════╗
║ TITLE (bold, primary color)       ║  ← Screen title
║ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━    ║  ← Visual separator
║                                   ║
║ Label: value                      ║  ← Regular text
║ • Bullet item                     ║  ← List items with bullet
║                                   ║
║ ⟳ Loading...                      ║  ← Status with icon
║ ✓ Success                         ║  ← Confirmation
║ ✘ Error message                   ║  ← Error with icon
║                                   ║
║ ↑↓ to navigate • Enter to select  ║  ← Hints (dimmed)
╚═══════════════════════════════════╝
```

### Text Styles

```typescript
// Titles - bold, primary color
<Text bold color="cyan">📨 NACHRICHTEN</Text>

// Labels - bold, regular color
<Text bold>Benutzername:</Text>

// Body text - regular
<Text>Enter your username</Text>

// Hints - dimmed
<Text dimColor>Press Esc to go back</Text>

// Status - colored with icon
<Text color="green">✓ Login successful</Text>
```

## 🎭 Icons & Symbols

Strategic use of Unicode symbols adds visual interest and scannability:

```typescript
export const icons = {
  check: '✓',           // Success/confirmation
  cross: '✘',           // Error/deletion
  arrow: '→',           // Direction
  bullet: '•',          // List items
  spinner: '⟳',         // Loading
  info: 'ⓘ',            // Information
  warning: '⚠',         // Warning
  error: '✘',           // Error
} as const;
```

### Emoji Shortcuts

- 📨 Messages - familiar envelope
- 📅 Calendar - universally recognized
- 🎓 Courses - graduation cap
- 👤 Profile - person silhouette
- ⚙️ Settings - gear icon
- 🚪 Logout - door icon

### Using Icons Effectively

```typescript
// Good: Icon + Text for clarity
<Text>📨 Messages (3)</Text>

// Good: Icon + Action indicator
<Text color="green">✓ Login successful</Text>

// Avoid: Icon alone without context
<Text>📨</Text>  // ← What does this mean?
```

## 🗂️ Layout & Spacing

### Boxes & Borders

```typescript
// Main screen borders - round style
<Box borderStyle="round" borderColor="cyan">
  {/* Content */}
</Box>

// Section separators
<Text>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</Text>

// Header boxes for emphasis
<Box borderStyle="round" borderColor={color} padding={1}>
  <Text bold color={color}>{title}</Text>
</Box>
```

### Spacing Rules

```typescript
// Vertical spacing
<Box marginBottom={1}>     // 1 line gap
<Box marginBottom={0.5}>   // half line gap
<Box marginTop={1}>        // top spacing

// Horizontal spacing
<Box marginRight={1}>      // right spacing
<Box marginLeft={1}>       // left spacing

// Padding (internal)
<Box padding={1}>          // 1 unit on all sides
<Box paddingX={1}>         // horizontal padding
<Box paddingY={1}>         // vertical padding
```

### Recommended Layout

```
┌──────────────────────────────┐
│ ✦ TITLE (marginBottom=1)     │
│ ━━━━━━━━━━━━━━━━━━━━━━━━━ │
│                              │  (padding on all)
│ Content here                 │
│ (marginBottom=1 between)     │
│                              │
│ [Selection Area]             │
│                              │
│ Hints (marginTop=1)          │
└──────────────────────────────┘
```

## 🎬 Animation & Motion

### Loading Spinner

```typescript
// Animated spinner frames
const frames = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'];

// Shows continuous animation every 80ms
<Text color="cyan">
  {frames[frame]} Loading...
</Text>
```

### State Transitions

- **Enter screen**: Show header, then content
- **Load data**: Show spinner, then replace with content
- **Error state**: Show error in red, keep previous content visible

## ✨ Component Patterns

### Card Component

```typescript
// Consistent card styling across screens
<Card title="Title" color="cyan" padding={1}>
  <ListItem label="Item 1" value="Value 1" />
  <ListItem label="Item 2" value="Value 2" />
</Card>
```

### Lists

```typescript
// Consistent list styling
<SelectInput
  items={menuItems}
  onSelect={handleSelect}
/>

// Visual feedback
// - Highlighted item shows cursor
// - Unselected items are dimmed
```

### Forms

```typescript
// Labeled input
<Box flexDirection="column" marginBottom={1}>
  <Text>Label:</Text>
  <TextInput
    value={value}
    onChange={setValue}
    placeholder="placeholder"
  />
</Box>

// Validation feedback (immediately after input)
{error && <Text color="red">✘ {error}</Text>}
```

## 📊 Status Indicators

### Login Screen Flow

```
┌─────────────────────────────┐
│ ✦ SCHULPORTAL HESSEN       │
│ ━━━━━━━━━━━━━━━━━━━━━━━   │
│                             │
│ Schul-ID:                   │
│ [input field]               │
│                             │
│ Enter zum Fortfahren        │
└─────────────────────────────┘

[Enter pressed]

┌─────────────────────────────┐
│ ✦ SCHULPORTAL HESSEN       │
│ ━━━━━━━━━━━━━━━━━━━━━━━   │
│                             │
│ Schul-ID: 1234              │
│ Benutzername:               │
│ [input field]               │
│                             │
│ Enter zum Fortfahren        │
└─────────────────────────────┘

[Loading]

┌─────────────────────────────┐
│ ⟳ Wird authentifiziert...  │
└─────────────────────────────┘

[Success]

✦ WILLKOMMEN
1234 • user.name
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📨 Nachrichten (3)
📅 Kalender (2)
🎓 Mein Unterricht
👤 Profil
...
```

## 🎨 Customization

### Changing Colors

Edit `src/theme.tsx`:

```typescript
export const colors = {
  primary: 'magenta',    // Change from cyan
  success: 'green',
  // ...
};
```

### Custom Components

Create in `src/components/`:

```typescript
export const MyComponent: React.FC = () => {
  return (
    <Box borderStyle="round" borderColor="cyan">
      <Text bold color="cyan">Title</Text>
    </Box>
  );
};
```

### Dark/Light Terminal Themes

The TUI automatically adapts to your terminal theme:
- **Dark terminals**: Bright colors work well
- **Light terminals**: Use dimColor for better contrast

## 🚀 Best Practices

1. **Consistent spacing** - Use predefined gap sizes
2. **Color meaning** - Green=good, red=bad, yellow=caution
3. **Icon clarity** - Always pair icons with text
4. **Readable hierarchy** - Main title → sections → items
5. **Loading feedback** - Always show spinners for async operations
6. **Error recovery** - Keep previous state when errors occur
7. **Keyboard focus** - Visual indicator of selected items
8. **Hint text** - Help users with dimmed instructions

## 📚 References

### Unicode Symbols
- Braille patterns: U+2800–U+28FF (for spinners)
- Box drawing: U+2500–U+257F
- Arrows: U+2190–U+21FF
- Symbols: U+2600–U+27BF

### Terminal ANSI Colors
- 0-7: Standard colors
- 8-15: Bright colors
- 16-231: 216 RGB colors
- 232-255: Grayscale

### Ink Documentation
- [Ink GitHub](https://github.com/vadimdemedes/ink)
- [Ink API](https://github.com/vadimdemedes/ink#api)
- [React Terminal Components](https://github.com/vadimdemedes/ink-select-input)

---

**Design is not decoration. It's communication.** ✨
