import { ReactNode, createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { User, api, setUnauthorizedHandler, tokenStore } from "./api";

interface AuthState {
  user: User | null;
  loading: boolean;
  signIn: (token: string, user: User) => void;
  signOut: () => void;
  refresh: () => Promise<void>;
}

const Ctx = createContext<AuthState>(null!);
export const useAuth = () => useContext(Ctx);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState<boolean>(!!tokenStore.get());

  const signOut = useCallback(() => { tokenStore.clear(); setUser(null); }, []);
  const signIn = useCallback((token: string, u: User) => { tokenStore.set(token); setUser(u); }, []);
  const refresh = useCallback(async () => { try { setUser(await api.me()); } catch { /* handled by 401 hook */ } }, []);

  useEffect(() => {
    setUnauthorizedHandler(signOut);
    if (!tokenStore.get()) return;
    api.me().then(setUser).catch(() => { /* 401 clears the token, network errors keep it */ }).finally(() => setLoading(false));
  }, [signOut]);

  const value = useMemo(() => ({ user, loading, signIn, signOut, refresh }), [user, loading, signIn, signOut, refresh]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
