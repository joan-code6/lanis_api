# Advanced TUI Development Guide

This guide explains how to extend the Schulportal Hessen TUI with custom features.

## 📋 Table of Contents

1. [Adding New Views](#adding-new-views)
2. [Extending the API Client](#extending-the-api-client)
3. [Styling & Theme](#styling--theme)
4. [State Management](#state-management)
5. [Advanced Patterns](#advanced-patterns)

## Adding New Views

### Step 1: Create a new component

Create a new file in `src/components/YourFeature.tsx`:

```typescript
import React from 'react';
import { Box, Text } from 'ink';
import SelectInput from 'ink-select-input';

interface YourFeatureViewProps {
  data: any[];
  isLoading: boolean;
  error?: string;
  onBack: () => void;
}

export const YourFeatureView: React.FC<YourFeatureViewProps> = ({
  data,
  isLoading,
  error,
  onBack,
}) => {
  const items = [
    { label: '← Back to Menu', value: 'back' },
  ];

  const handleSelect = (item: { value: string }) => {
    if (item.value === 'back') {
      onBack();
    }
  };

  return (
    <Box flexDirection="column" padding={1} borderStyle="round" borderColor="green">
      <Box marginBottom={1}>
        <Text bold color="green">
          🎯 YOUR FEATURE
        </Text>
      </Box>

      {isLoading && <Text color="yellow">Loading...</Text>}
      {error && <Text color="red">Error: {error}</Text>}

      <Box marginBottom={1}>
        <SelectInput items={items} onSelect={handleSelect} />
      </Box>
    </Box>
  );
};
```

### Step 2: Update the API client

Add the method to `src/api.ts`:

```typescript
async getYourData(): Promise<any[]> {
  try {
    const response = await axios.get(`${BASE_URL}/your-endpoint`, {
      headers: this.getHeaders(),
    });
    return response.data;
  } catch (error) {
    throw new Error(`Failed to fetch your data: ${error}`);
  }
}
```

### Step 3: Update types

Add to `src/types.ts`:

```typescript
export interface YourDataType {
  id: string;
  name: string;
  // ... other fields
}

export type Screen = 'login' | 'dashboard' | 'your-feature' | /* ... */;
```

### Step 4: Update App.tsx

Import and integrate:

```typescript
import { YourFeatureView } from './components/YourFeature';

// Add state
const [yourData, setYourData] = useState<YourDataType[]>([]);

// Add handler
const handleSelectModule = async (module: string) => {
  switch (module) {
    case 'your-feature':
      setScreen('your-feature');
      setIsLoading(true);
      try {
        const data = await api.getYourData();
        setYourData(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load');
      } finally {
        setIsLoading(false);
      }
      break;
    // ... other cases
  }
};

// Add to JSX
{screen === 'your-feature' && (
  <YourFeatureView
    data={yourData}
    isLoading={isLoading}
    error={error}
    onBack={handleBack}
  />
)}
```

### Step 5: Update Dashboard

Add menu item to Dashboard component:

```typescript
const menuItems = [
  // ... existing items
  { label: '🎯 Your Feature', value: 'your-feature' },
];
```

## Extending the API Client

### Adding Authentication Methods

```typescript
// In api.ts
async refreshToken(currentToken: string): Promise<string> {
  try {
    const response = await axios.post(`${BASE_URL}/refresh-token`, {
      token: currentToken,
    });
    this.sessionToken = response.data.token;
    return response.data.token;
  } catch (error) {
    throw new Error('Token refresh failed');
  }
}
```

### Adding Batch Operations

```typescript
async getMultipleUsers(userIds: string[]): Promise<User[]> {
  try {
    const response = await axios.post(`${BASE_URL}/users/batch`, {
      ids: userIds,
    }, { headers: this.getHeaders() });
    return response.data;
  } catch (error) {
    throw new Error('Batch fetch failed');
  }
}
```

### Caching Responses

```typescript
private cache = new Map<string, { data: any; timestamp: number }>();
private CACHE_TTL = 5 * 60 * 1000; // 5 minutes

async getCachedData(key: string, fetcher: () => Promise<any>): Promise<any> {
  const cached = this.cache.get(key);
  
  if (cached && Date.now() - cached.timestamp < this.CACHE_TTL) {
    return cached.data;
  }
  
  const data = await fetcher();
  this.cache.set(key, { data, timestamp: Date.now() });
  return data;
}
```

## Styling & Theme

### Using the Theme System

```typescript
import { colors, icons, Border, Divider, StatusBadge } from '../theme';
import { Card, ListItem, VStack, HStack } from '../components/Layout';

export const Example = () => (
  <VStack gap={1}>
    <Card title="My Title" color={colors.primary}>
      <ListItem label="Item 1" value="Value 1" color={colors.success} />
      <ListItem label="Item 2" value="Value 2" color={colors.warning} />
    </Card>
    
    <Divider />
    
    <HStack gap={2}>
      <StatusBadge status="success" label="Online" />
      <StatusBadge status="pending" label="Syncing" />
    </HStack>
  </VStack>
);
```

### Creating Custom Colors

```typescript
import { Text, Box } from 'ink';

// Use named colors
<Text color="cyan">Cyan text</Text>
<Text color="rgb(100,100,100)">RGB color</Text>

// Use background colors
<Text backgroundColor="cyan">Text with background</Text>
```

### Custom Components

```typescript
import React from 'react';
import { Box, Text } from 'ink';

interface HeaderProps {
  title: string;
  subtitle?: string;
}

export const Header: React.FC<HeaderProps> = ({ title, subtitle }) => (
  <Box flexDirection="column" marginBottom={1} borderStyle="round" borderColor="cyan">
    <Text bold color="cyan" fontSize="large">
      {title}
    </Text>
    {subtitle && (
      <Text color="gray" fontSize="small">
        {subtitle}
      </Text>
    )}
  </Box>
);
```

## State Management

### Using Hooks Effectively

```typescript
const [data, setData] = useState<Data[]>([]);
const [loading, setLoading] = useState(false);
const [error, setError] = useState<string>('');

// Fetch with error handling
useEffect(() => {
  const fetch = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await api.getData();
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unknown error');
    } finally {
      setLoading(false);
    }
  };

  fetch();
}, []); // Run once on mount
```

### Context for Global State

```typescript
import React, { createContext, useState } from 'react';

interface AuthContext {
  session: SessionState | null;
  setSession: (session: SessionState | null) => void;
}

export const AuthContext = createContext<AuthContext | null>(null);

export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [session, setSession] = useState<SessionState | null>(null);

  return (
    <AuthContext.Provider value={{ session, setSession }}>
      {children}
    </AuthContext.Provider>
  );
};

// Usage in component
import { useContext } from 'react';

const MyComponent = () => {
  const auth = useContext(AuthContext);
  return <Text>{auth?.session?.username}</Text>;
};
```

## Advanced Patterns

### Custom Hooks

```typescript
// useApi.ts
import { useState, useCallback } from 'react';

interface UseApiState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

export function useApi<T>(
  apiCall: () => Promise<T>,
  immediate = true
): UseApiState<T> & { refetch: () => Promise<void> } {
  const [state, setState] = useState<UseApiState<T>>({
    data: null,
    loading: immediate,
    error: null,
  });

  const refetch = useCallback(async () => {
    setState({ data: null, loading: true, error: null });
    try {
      const result = await apiCall();
      setState({ data: result, loading: false, error: null });
    } catch (error) {
      setState({
        data: null,
        loading: false,
        error: error instanceof Error ? error.message : 'Unknown error',
      });
    }
  }, [apiCall]);

  return { ...state, refetch };
}

// Usage
const MyComponent = () => {
  const { data, loading, error, refetch } = useApi(() => api.getData());
  
  return (
    <Box>
      {loading && <Text>Loading...</Text>}
      {error && <Text color="red">{error}</Text>}
      {data && <Text>{data}</Text>}
    </Box>
  );
};
```

### Pagination

```typescript
interface PaginationState {
  page: number;
  pageSize: number;
  total: number;
}

const usePagination = (total: number, pageSize = 10) => {
  const [page, setPage] = useState(1);
  
  const canNext = page * pageSize < total;
  const canPrev = page > 1;
  
  return {
    page,
    pageSize,
    nextPage: () => canNext && setPage(p => p + 1),
    prevPage: () => canPrev && setPage(p => p - 1),
    canNext,
    canPrev,
    offset: (page - 1) * pageSize,
  };
};
```

### Search & Filter

```typescript
const useSearch = (items: any[], searchKey: string) => {
  const [query, setQuery] = useState('');
  
  const results = query.trim() === ''
    ? items
    : items.filter(item =>
        item[searchKey].toLowerCase().includes(query.toLowerCase())
      );
  
  return { query, setQuery, results };
};
```

## Testing Components

```typescript
// YourFeature.test.tsx
import React from 'react';
import { render } from 'ink-testing-library';
import { YourFeatureView } from './YourFeature';

describe('YourFeatureView', () => {
  it('should render loading state', () => {
    const { lastFrame } = render(
      <YourFeatureView
        data={[]}
        isLoading={true}
        onBack={() => {}}
      />
    );
    
    expect(lastFrame()).toMatch('Loading');
  });

  it('should render error state', () => {
    const { lastFrame } = render(
      <YourFeatureView
        data={[]}
        isLoading={false}
        error="Test error"
        onBack={() => {}}
      />
    );
    
    expect(lastFrame()).toMatch('Error: Test error');
  });
});
```

## Performance Tips

1. **Memoize components** to avoid unnecessary re-renders:
```typescript
const MemoizedComponent = React.memo(YourComponent);
```

2. **Use `useCallback`** for stable function references:
```typescript
const handleClick = useCallback(() => { /* ... */ }, [deps]);
```

3. **Cache API responses** to reduce requests
4. **Lazy load data** only when needed
5. **Use `useMemo`** for expensive computations

## Deployment

### Build for Production

```bash
npm run build
```

### Package as Executable

```bash
pkg dist/index.js --output lanis-cli
```

### Docker

```dockerfile
FROM node:18-alpine
WORKDIR /app
COPY . .
RUN npm ci && npm run build
ENTRYPOINT ["node", "dist/index.js"]
```

---

For more help, check the [main README](README_TUI.md) or open an issue!
