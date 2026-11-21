import {lazy, StrictMode, Suspense} from 'react';
import ReactDOM from 'react-dom/client';
import {MantineProvider} from '@mantine/core';
import {BrowserRouter, Route, Routes} from 'react-router-dom';
import '@mantine/core/styles.css';
import './style.css';
import './redesign.css';
import {PageErrorBoundary} from './PageErrorBoundary';
import {ProductHome} from './ProductHome';
import {theme} from './theme';

const Workspace = lazy(() => import('./App'));

ReactDOM.createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="light">
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<ProductHome/>}/>
          <Route path="/*" element={
            <PageErrorBoundary onRetry={() => window.location.reload()}>
              <Suspense fallback={<main><p role="status">Opening workspace…</p></main>}>
                <Workspace/>
              </Suspense>
            </PageErrorBoundary>
          }/>
        </Routes>
      </BrowserRouter>
    </MantineProvider>
  </StrictMode>
);
