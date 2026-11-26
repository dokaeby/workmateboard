import { Suspense } from "react";
import type { ViewKey } from "../shared/api/types";
import { Spinner } from "../shared/ui";
import { VIEW_COMPONENTS } from "./viewComponents";

/**
 * 뷰 레지스트리 — 각 feature의 페이지 컴포넌트를 React.lazy로 지연 로딩.
 * 초기 번들에는 셸만 포함되고 뷰 청크는 최초 진입 시 로드된다.
 * 새 기능 추가: features/<name>/index.tsx에 default export 컴포넌트를 두고
 * viewComponents.ts의 VIEW_COMPONENTS에 등록한다.
 */
export function ViewLoader({ view }: { view: ViewKey }) {
  const C = VIEW_COMPONENTS[view];
  if (!C) return null;
  return (
    <Suspense fallback={<Spinner />}>
      <C />
    </Suspense>
  );
}
