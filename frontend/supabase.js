// =============================================================
// supabase.js — Supabase client, auth helpers, data persistence
// Supports real Supabase backend + smooth local fallback
// =============================================================

// ─── Configuration ──────────────────────────────────────────
// To connect to your Supabase project:
// 1. Go to https://supabase.com → Settings → API
// 2. Paste your Project URL and Anon Key below
const SUPABASE_URL = 'https://YOUR_PROJECT_ID.supabase.co';
const SUPABASE_ANON_KEY = 'YOUR_ANON_KEY';

const EMAIL_DOMAIN = 'codebase-rag.app';

// Helper to check if real Supabase credentials are configured
function isSupabaseConfigured() {
    return (
        SUPABASE_URL &&
        SUPABASE_ANON_KEY &&
        !SUPABASE_URL.includes('YOUR_PROJECT_ID') &&
        !SUPABASE_ANON_KEY.includes('YOUR_ANON_KEY')
    );
}

// ─── Client Init ────────────────────────────────────────────
let supabaseClient = null;

function getSupabase() {
    if (!isSupabaseConfigured()) {
        return null;
    }
    if (!supabaseClient) {
        supabaseClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
    }
    return supabaseClient;
}

// ─── Local Storage Fallback Store ───────────────────────────
const LOCAL_USERS_KEY = 'codebase_rag_users';
const LOCAL_CURRENT_USER_KEY = 'codebase_rag_current_user';
const LOCAL_CONVERSATIONS_PREFIX = 'codebase_rag_conversations_';

function getLocalUsers() {
    try {
        return JSON.parse(localStorage.getItem(LOCAL_USERS_KEY) || '[]');
    } catch {
        return [];
    }
}

function saveLocalUsers(users) {
    localStorage.setItem(LOCAL_USERS_KEY, JSON.stringify(users));
}

function getLocalCurrentUser() {
    try {
        return JSON.parse(localStorage.getItem(LOCAL_CURRENT_USER_KEY) || 'null');
    } catch {
        return null;
    }
}

function setLocalCurrentUser(user) {
    if (user) {
        localStorage.setItem(LOCAL_CURRENT_USER_KEY, JSON.stringify(user));
    } else {
        localStorage.removeItem(LOCAL_CURRENT_USER_KEY);
    }
}

// ─── Auth Helpers ───────────────────────────────────────────

function usernameToEmail(username) {
    return `${username.toLowerCase().trim()}@${EMAIL_DOMAIN}`;
}

function emailToUsername(email) {
    if (!email) return 'User';
    return email.split('@')[0];
}

async function signUp(username, password) {
    const sb = getSupabase();
    const cleanUser = username.trim();
    const email = usernameToEmail(cleanUser);

    if (sb) {
        try {
            const { data, error } = await sb.auth.signUp({
                email,
                password,
                options: {
                    data: { username: cleanUser }
                }
            });
            if (error) throw error;
            return data;
        } catch (err) {
            if (err.message === 'Failed to fetch') {
                throw new Error('Failed to connect to Supabase. Check your SUPABASE_URL in frontend/supabase.js');
            }
            throw err;
        }
    }

    // Local Storage Fallback
    const users = getLocalUsers();
    const existing = users.find(
        u => u.username?.toLowerCase() === cleanUser.toLowerCase() ||
            u.user_metadata?.username?.toLowerCase() === cleanUser.toLowerCase()
    );
    if (existing) {
        throw new Error('User already registered. Please sign in instead.');
    }

    const newUser = {
        id: 'user_' + Date.now() + '_' + Math.random().toString(36).substr(2, 4),
        email,
        user_metadata: { username: cleanUser },
        password, // stored locally for demo authentication
        created_at: new Date().toISOString()
    };

    users.push(newUser);
    saveLocalUsers(users);

    const sessionUser = {
        id: newUser.id,
        email: newUser.email,
        user_metadata: newUser.user_metadata
    };
    setLocalCurrentUser(sessionUser);

    return { user: sessionUser, session: { user: sessionUser } };
}

async function signIn(username, password) {
    const sb = getSupabase();
    const cleanUser = username.trim();
    const email = usernameToEmail(cleanUser);

    if (sb) {
        try {
            const { data, error } = await sb.auth.signInWithPassword({
                email,
                password
            });
            if (error) throw error;
            return data;
        } catch (err) {
            if (err.message === 'Failed to fetch') {
                throw new Error('Failed to connect to Supabase. Check your SUPABASE_URL in frontend/supabase.js');
            }
            throw err;
        }
    }

    // Local Storage Fallback
    const users = getLocalUsers();
    const found = users.find(
        u => u.username?.toLowerCase() === cleanUser.toLowerCase() ||
            u.user_metadata?.username?.toLowerCase() === cleanUser.toLowerCase()
    );

    if (!found || found.password !== password) {
        throw new Error('Invalid login credentials.');
    }

    const sessionUser = {
        id: found.id,
        email: found.email,
        user_metadata: found.user_metadata
    };
    setLocalCurrentUser(sessionUser);

    return { user: sessionUser, session: { user: sessionUser } };
}

async function signOut() {
    const sb = getSupabase();
    if (sb) {
        try {
            await sb.auth.signOut();
        } catch (err) {
            console.warn('Supabase signout notice:', err);
        }
    }
    setLocalCurrentUser(null);
}

async function getSession() {
    const sb = getSupabase();
    if (sb) {
        try {
            const { data: { session } } = await sb.auth.getSession();
            if (session) return session;
        } catch (err) {
            console.warn('Supabase getSession fallback to local:', err);
        }
    }
    const localUser = getLocalCurrentUser();
    if (localUser) {
        return { user: localUser };
    }
    return null;
}

async function getUser() {
    const session = await getSession();
    return session?.user || null;
}

function onAuthStateChange(callback) {
    const sb = getSupabase();
    if (sb) {
        return sb.auth.onAuthStateChange((event, session) => {
            callback(event, session);
        });
    }
    return { data: { subscription: { unsubscribe: () => { } } } };
}

// ─── Account Management ────────────────────────────────────

async function deleteAccount() {
    const user = await getUser();
    if (!user) throw new Error('Not authenticated');

    // Delete all user conversations
    await deleteAllConversations(user.id);

    // Remove user from local users store if using local fallback
    if (!isSupabaseConfigured()) {
        const users = getLocalUsers().filter(u => u.id !== user.id);
        saveLocalUsers(users);
    }

    await signOut();
}

// ─── Conversation Persistence ──────────────────────────────

async function saveConversation(chatData) {
    const user = await getUser();
    if (!user) return;

    const sb = getSupabase();
    if (sb) {
        try {
            const { error } = await sb
                .from('conversations')
                .upsert({
                    id: chatData.id,
                    user_id: user.id,
                    repo_url: chatData.repoUrl,
                    messages: chatData.messages,
                    updated_at: new Date().toISOString()
                }, { onConflict: 'id' });

            if (error) console.error('Failed to save conversation to Supabase:', error);
            return;
        } catch (err) {
            console.warn('Supabase save error, writing to local storage fallback:', err);
        }
    }

    // Local Storage Fallback
    const key = LOCAL_CONVERSATIONS_PREFIX + user.id;
    try {
        const stored = JSON.parse(localStorage.getItem(key) || '[]');
        const idx = stored.findIndex(c => c.id === chatData.id);
        const item = {
            id: chatData.id,
            repoUrl: chatData.repoUrl,
            messages: chatData.messages,
            updatedAt: new Date().toISOString()
        };
        if (idx >= 0) {
            stored[idx] = item;
        } else {
            stored.unshift(item);
        }
        localStorage.setItem(key, JSON.stringify(stored));
    } catch (err) {
        console.error('Failed to save conversation locally:', err);
    }
}

async function loadConversations() {
    const user = await getUser();
    if (!user) return [];

    const sb = getSupabase();
    if (sb) {
        try {
            const { data, error } = await sb
                .from('conversations')
                .select('*')
                .eq('user_id', user.id)
                .order('updated_at', { ascending: false });

            if (!error && data) {
                return data.map(row => ({
                    id: row.id,
                    repoUrl: row.repo_url,
                    messages: row.messages || [],
                }));
            }
        } catch (err) {
            console.warn('Supabase load error, checking local storage fallback:', err);
        }
    }

    // Local Storage Fallback
    const key = LOCAL_CONVERSATIONS_PREFIX + user.id;
    try {
        const stored = JSON.parse(localStorage.getItem(key) || '[]');
        return stored.map(item => ({
            id: item.id,
            repoUrl: item.repoUrl,
            messages: item.messages || []
        }));
    } catch (err) {
        console.error('Failed to load local conversations:', err);
        return [];
    }
}

async function deleteConversation(chatId) {
    const user = await getUser();
    if (!user) return;

    const sb = getSupabase();
    if (sb) {
        try {
            await sb.from('conversations').delete().eq('id', chatId).eq('user_id', user.id);
        } catch (err) {
            console.warn('Supabase delete conversation error:', err);
        }
    }

    // Always clear from local storage as well
    const key = LOCAL_CONVERSATIONS_PREFIX + user.id;
    try {
        const stored = JSON.parse(localStorage.getItem(key) || '[]').filter(c => c.id !== chatId);
        localStorage.setItem(key, JSON.stringify(stored));
    } catch (err) {
        console.error('Failed to delete local conversation:', err);
    }
}

async function deleteAllConversations(userId) {
    const sb = getSupabase();
    if (sb) {
        try {
            await sb.from('conversations').delete().eq('user_id', userId);
        } catch (err) {
            console.warn('Supabase delete all conversations error:', err);
        }
    }

    const key = LOCAL_CONVERSATIONS_PREFIX + userId;
    localStorage.removeItem(key);
}