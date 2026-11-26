/**
 * vitest setup — node 환경에서 Node 내장 experimental localStorage 접근 시
 * ExperimentalWarning(--localstorage-file 미제공)이 발생하므로,
 * 테스트용 인메모리 localStorage 스텁으로 교체한다.
 */
const store = new Map<string, string>();

const localStorageStub: Storage = {
  get length() {
    return store.size;
  },
  clear: () => store.clear(),
  getItem: (key: string) => store.get(key) ?? null,
  key: (index: number) => [...store.keys()][index] ?? null,
  removeItem: (key: string) => void store.delete(key),
  setItem: (key: string, value: string) => void store.set(key, String(value)),
};

Object.defineProperty(globalThis, "localStorage", {
  value: localStorageStub,
  configurable: true,
  writable: true,
});
