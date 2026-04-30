/**
 * Storage utilities for persisting credentials and preferences
 * Uses filesystem storage for Node.js CLI
 */

import fs from 'fs';
import path from 'path';
import os from 'os';

const STORAGE_DIR = path.join(os.homedir(), '.lanis');
const CREDS_FILE = path.join(STORAGE_DIR, 'creds.json');
const PREFS_FILE = path.join(STORAGE_DIR, 'prefs.json');

// Ensure storage directory exists
if (!fs.existsSync(STORAGE_DIR)) {
  fs.mkdirSync(STORAGE_DIR, { recursive: true });
}

export interface StoredCredentials {
  schoolId: string;
  username: string;
  password: string;
}

export const storage = {
  // Credentials
  getCredentials: (): StoredCredentials | null => {
    try {
      if (fs.existsSync(CREDS_FILE)) {
        const data = fs.readFileSync(CREDS_FILE, 'utf-8');
        return JSON.parse(data);
      }
      return null;
    } catch {
      return null;
    }
  },

  saveCredentials: (creds: StoredCredentials) => {
    try {
      fs.writeFileSync(CREDS_FILE, JSON.stringify(creds), 'utf-8');
    } catch (error) {
      console.error('Failed to save credentials:', error);
    }
  },

  clearCredentials: () => {
    try {
      if (fs.existsSync(CREDS_FILE)) {
        fs.unlinkSync(CREDS_FILE);
      }
    } catch {
      // ignore
    }
  },

  // Preferences
  getPreferences: () => {
    try {
      if (fs.existsSync(PREFS_FILE)) {
        const data = fs.readFileSync(PREFS_FILE, 'utf-8');
        return JSON.parse(data);
      }
      return { rememberMe: false };
    } catch {
      return { rememberMe: false };
    }
  },

  setPreference: (key: string, value: any) => {
    try {
      const prefs = storage.getPreferences();
      prefs[key] = value;
      fs.writeFileSync(PREFS_FILE, JSON.stringify(prefs), 'utf-8');
    } catch {
      // ignore
    }
  },
};
