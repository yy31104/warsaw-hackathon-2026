import {useMutation, useQueryClient} from '@tanstack/react-query';
import {request} from './api';

type ReceiptStep = {
  run_id?: number | null;
  requested_by?: string | null;
  approvers?: string[];
};
type ReceiptResult = {state: 'succeeded' | 'failed' | 'unknown'; message?: string};

export function CheckReceipt({step, actor, onChecked}: {
  step: ReceiptStep; actor: string; onChecked?: (state: ReceiptResult['state']) => void;
}) {
  const client = useQueryClient();
  const check = useMutation({
    mutationFn: () => request<ReceiptResult>(`/action-runs/${step.run_id}/reconcile`, actor, undefined, 'POST'),
    onSuccess: async result => {
      onChecked?.(result.state);
      await Promise.all([
        client.invalidateQueries({queryKey: ['detail']}),
        client.invalidateQueries({queryKey: ['alerts']}),
      ]);
    },
  });
  if (!step.run_id || (actor !== step.requested_by && !step.approvers?.includes(actor))) return null;
  return <div>
    <div className="cf-actions">
      <button type="button" className="cf-btn" disabled={check.isPending} onClick={() => check.mutate()}>
        Check receipt
      </button>
      <span className="cf-meta">Receipt <code>action-{step.run_id}</code></span>
    </div>
    {check.data?.state === 'unknown' && check.data.message && <p role="status">{check.data.message}</p>}
    {check.error && <p role="alert">{check.error.message}</p>}
  </div>;
}
