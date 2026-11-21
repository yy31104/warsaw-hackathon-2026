export function FunnelMark() {
  return <span className="funnel-mark" aria-hidden="true"><i/><i/><i/></span>;
}

export function Glyph({name}: {name: string}) {
  return <span aria-hidden="true" className={`ui-glyph glyph-${name}`}><i/><i/><i/><i/></span>;
}
