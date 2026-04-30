import axios from 'axios';
import { LoginCredentials, SessionState, AppModule, Message, CalendarEvent, CourseInfo } from './types';

const BASE_URL = process.env.API_URL || 'http://localhost:8000';

class LanisAPI {
  private sessionToken: string | null = null;

  setToken(token: string) {
    this.sessionToken = token;
  }

  private getHeaders() {
    return {
      'X-Session-Token': this.sessionToken || '',
      'Content-Type': 'application/json',
    };
  }

  async login(credentials: LoginCredentials): Promise<SessionState> {
    try {
      const response = await axios.post(`${BASE_URL}/login`, {
        school_id: credentials.schoolId,
        username: credentials.username,
        password: credentials.password,
      });
      
      this.sessionToken = response.data.token;
      return response.data;
    } catch (error) {
      throw new Error(`Login failed: ${error instanceof Error ? error.message : 'Unknown error'}`);
    }
  }

  async logout(): Promise<void> {
    try {
      await axios.post(`${BASE_URL}/logout`, {}, { headers: this.getHeaders() });
      this.sessionToken = null;
    } catch (error) {
      throw new Error(`Logout failed: ${error instanceof Error ? error.message : 'Unknown error'}`);
    }
  }

  async getModules(): Promise<AppModule[]> {
    try {
      const response = await axios.get(`${BASE_URL}/modules`, { headers: this.getHeaders() });
      return response.data;
    } catch (error) {
      throw new Error(`Failed to fetch modules: ${error instanceof Error ? error.message : 'Unknown error'}`);
    }
  }

  async getMessages(type: string = 'All'): Promise<Message[]> {
    try {
      const validTypes = ['All', 'visibleOnly', 'unvisibleOnly'];
      const normalizedType = validTypes.includes(type) ? type : 'All';
      
      const response = await axios.get(`${BASE_URL}/nachrichten/headers`, {
        params: { get_type: normalizedType },
        headers: this.getHeaders(),
      });
      console.log('[API] Messages response:', response.data);
      
      if (response.data?.success === false) {
        console.error('[API] Messages error:', response.data.error);
        return [];
      }
      
      if (response.data?.conversations && Array.isArray(response.data.conversations)) {
        return response.data.conversations.map((conv: any) => {
          const senderName = conv.SenderName 
            ? conv.SenderName.replace(/<[^>]*>/g, '').trim() 
            : conv.Sender || 'Unbekannt';
          return {
            id: conv.Id || conv.id || '',
            sender: senderName,
            subject: conv.Betreff || conv.subject || 'Kein Betreff',
            timestamp: conv.Datum || conv.DatumUnix || '',
            raw: conv,
          };
        });
      }
      console.log('[API] No conversations found in response');
      return [];
    } catch (error) {
      console.error('[API] getMessages error:', error);
      return [];
    }
  }

  async getMessageContent(conversationId: string): Promise<any> {
    try {
      const response = await axios.get(`${BASE_URL}/nachrichten/${conversationId}`, {
        headers: this.getHeaders(),
      });
      console.log('[API] Message content response:', response.data);
      
      if (response.data?.success === false) {
        console.error('[API] Message content error:', response.data.error);
        return null;
      }
      
      return response.data;
    } catch (error) {
      console.error('[API] getMessageContent error:', error);
      return null;
    }
  }

  async getCalendarEvents(): Promise<CalendarEvent[]> {
    try {
      const response = await axios.get(`${BASE_URL}/kalender`, { headers: this.getHeaders() });
      // Extract events from wrapped response structure
      // Try different possible formats
      if (Array.isArray(response.data?.events)) {
        return response.data.events;
      }
      if (Array.isArray(response.data?.calendar)) {
        return response.data.calendar;
      }
      if (response.data?.calendar && typeof response.data.calendar === 'object') {
        // Parse calendar object if it has entries
        const events = (response.data.calendar as any).events || [];
        return Array.isArray(events) ? events : [];
      }
      return [];
    } catch (error) {
      return [];
    }
  }

  async getCourses(): Promise<CourseInfo[]> {
    try {
      const response = await axios.get(`${BASE_URL}/meinunterricht`, { headers: this.getHeaders() });
      // Extract entries from wrapped response: { success: true, entries: [...] }
      console.log('[API] Courses response:', response.data);
      if (response.data?.entries && Array.isArray(response.data.entries)) {
        const courses = response.data.entries.map((entry: any) => ({
          id: entry.book_id || entry.entry_id || entry.id || 'unknown',
          name: entry.name || 'Unbekannter Kurs',
          teacher: entry.teacher_full_name || entry.teacher || entry.teacher_short || 'Unbekannt',
          topic: entry.thema || '',
          date: entry.datum || '',
          homework: entry.homework || '',
          homeworkDone: entry.homework_done || false,
          courseLink: entry.course_link || '',
        }));
        console.log('[API] Extracted courses:', courses);
        return courses;
      }
      console.log('[API] No entries found in response');
      return [];
    } catch (error) {
      console.error('[API] getCourses error:', error);
      return [];
    }
  }

  async getCourseDetails(courseId: string): Promise<any> {
    try {
      const response = await axios.get(`${BASE_URL}/meinunterricht/course/${courseId}`, {
        headers: this.getHeaders(),
      });
      console.log('[API] Course details response:', response.data);
      return response.data;
    } catch (error) {
      console.error('[API] getCourseDetails error:', error);
      return null;
    }
  }

  async setHomeworkDone(courseId: string, entryId: string, done: boolean): Promise<boolean> {
    try {
      const response = await axios.post(
        `${BASE_URL}/meinunterricht/homework-done`,
        new URLSearchParams({
          course_id: courseId,
          entry_id: entryId,
          done: done ? 'true' : 'false',
        }),
        {
          headers: { 
            ...this.getHeaders(),
            'Content-Type': 'application/x-www-form-urlencoded',
          },
        }
      );
      console.log('[API] Set homework done response:', response.data);
      return response.data?.success === true;
    } catch (error) {
      console.error('[API] setHomeworkDone error:', error);
      return false;
    }
  }

  async getUserProfile() {
    try {
      const response = await axios.get(`${BASE_URL}/benutzer`, { headers: this.getHeaders() });
      return response.data;
    } catch (error) {
      throw new Error(`Failed to fetch profile: ${error instanceof Error ? error.message : 'Unknown error'}`);
    }
  }
}

export const api = new LanisAPI();
