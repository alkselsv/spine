import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { WorkspaceLayout } from './WorkspaceLayout';

afterEach(cleanup);

describe('master-detail workspace', () => {
  it('test_master_and_detail_content_have_one_named_workspace', () => {
    render(
      <WorkspaceLayout
        ariaLabel="Synthetic Q&A workspace"
        detail={<div>Selected detail</div>}
        master={<div>Canonical master list</div>}
      />,
    );

    expect(screen.getByRole('region', { name: 'Synthetic Q&A workspace' })).toBeVisible();
    expect(screen.getByText('Canonical master list')).toBeInTheDocument();
    expect(screen.getByText('Selected detail')).toBeInTheDocument();
  });

  it('test_supplied_inspector_is_exposed_as_complementary_content', () => {
    render(
      <WorkspaceLayout
        ariaLabel="Synthetic Q&A workspace"
        detail={<div>Selected detail</div>}
        inspector={<div>Protected inspector host</div>}
        master={<div>Canonical master list</div>}
      />,
    );

    const inspector = screen.getByRole('complementary');
    expect(inspector).toContainElement(screen.getByText('Protected inspector host'));
  });

  it('test_omitted_inspector_does_not_create_empty_complementary_content', () => {
    render(
      <WorkspaceLayout
        ariaLabel="Synthetic Q&A workspace"
        detail={<div>Selected detail</div>}
        master={<div>Canonical master list</div>}
      />,
    );

    expect(screen.queryByRole('complementary')).not.toBeInTheDocument();
  });
});
