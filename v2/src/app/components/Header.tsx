import { useAppStore } from "../../shared/store/appStore";
import { LangToggle } from "./LangToggle";
import { HeaderActions } from "./HeaderActions";

/** 모바일 상단바 — 레거시 app-header의 모바일 대응 (☰ + 브랜드 + 액션 아이콘). */
export function Header() {
  const { toggleSidebar } = useAppStore();
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b border-border bg-surface/90 px-3 backdrop-blur lg:hidden">
      <button className="rounded-md p-2 hover:bg-surface-hover" onClick={toggleSidebar} aria-label="menu">☰</button>
      <span className="font-bold text-accent">jobMate</span>
      <div className="ml-auto flex items-center gap-0.5"><LangToggle /><HeaderActions /></div>
    </header>
  );
}
