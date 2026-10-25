import type { ReactNode } from 'react';

import styles from './WorkspaceLayout.module.css';

export function WorkspaceLayout({
  ariaLabel,
  detail,
  inspector,
  inspectorOpen = false,
  master,
  masterCollapsed = false,
  mobileDetailActive = false,
}: {
  ariaLabel: string;
  detail: ReactNode;
  inspector?: ReactNode;
  inspectorOpen?: boolean;
  master: ReactNode;
  masterCollapsed?: boolean;
  mobileDetailActive?: boolean;
}) {
  return (
    <section
      aria-label={ariaLabel}
      className={styles.workspace}
      data-has-inspector={Boolean(inspector)}
      data-inspector-open={inspectorOpen}
      data-master-collapsed={masterCollapsed}
      data-mobile-detail={mobileDetailActive}
    >
      <div className={styles.master}>{master}</div>
      <div className={styles.detail}>{detail}</div>
      {inspector ? <aside className={styles.inspector}>{inspector}</aside> : null}
    </section>
  );
}
