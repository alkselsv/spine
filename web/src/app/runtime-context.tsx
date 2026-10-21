import { createContext, useContext, type ReactNode } from 'react';

import { readBrowserRuntime, type BrowserRuntime } from './runtime';

const BrowserRuntimeContext = createContext<BrowserRuntime>(readBrowserRuntime({}));

export function BrowserRuntimeProvider({
  children,
  value,
}: {
  children: ReactNode;
  value: BrowserRuntime;
}) {
  return <BrowserRuntimeContext.Provider value={value}>{children}</BrowserRuntimeContext.Provider>;
}

export function useBrowserRuntime(): BrowserRuntime {
  return useContext(BrowserRuntimeContext);
}
