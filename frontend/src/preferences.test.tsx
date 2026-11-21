import {MantineProvider} from '@mantine/core';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {renderToStaticMarkup} from 'react-dom/server';
import {expect, it} from 'vitest';
import {People} from './Screens';

it('renders preferences when a configured team has no approver list', () => {
  const html = renderToStaticMarkup(
    <MantineProvider>
      <QueryClientProvider client={new QueryClient()}>
        <People/>
      </QueryClientProvider>
    </MantineProvider>
  );
  expect(html).toContain('My notification preferences');
  expect(html).toContain('Approvers: None assigned');
  expect(html).toContain('Approvers: lee');
});
