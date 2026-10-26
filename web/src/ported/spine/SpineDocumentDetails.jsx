import { useState } from "react";
import { FileUp, RotateCcw, Trash2, X } from "lucide-react";
import {
  AlertBanner, Button, Caption, Card, CardHeader, Divider, IconButton, Sheet,
  SubtleTabs, Text, motion, tokens, usePal,
} from "../halaska-kit";
import { CopyField, IssueSummary, KeyValues, PipelineSteps, SectionTitle, StateMark } from "./SpineParts";
import {
  admissionLabel, coverageLabel, documentIssue, documentState, formatDate, formatRelative, freshnessLabel, healthLabel, idTail, locatorLabel,
  metadataState, optionLabel, pipelineStage, reasonLabel, shortId, sourceKindLabel,
} from "./spineData";
import { ru } from "./ru";

const D = ru.documents;
const TABS = [D.overviewTab, D.revisionTimeline, D.contentInspectionTab];

const detailsTitle = (doc) => `${sourceKindLabel(doc.source_kind)} · …${idTail(doc.source_object_id)}`;

const STAGE_CAPTION = {
  uploaded: "Ревизия получена и ждёт решения о допуске.",
  parsing: D.upload.processingDescription,
  indexing: D.upload.indexingDescription,
};

function Overview({ doc, onCopy, onCommand }) {
  const state = documentState(doc);
  const stage = pipelineStage(doc);
  const issue = documentIssue(doc);
  const rev = doc.current_revision;
  const meta = doc.operational_metadata ?? {};
  const metaState = metadataState(doc);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <StateMark tone={state.tone} label={state.label} pulse={state.pulse} />
        <Caption>{D.columns.updated} {formatRelative(rev?.observed_at ?? doc.created_at)}</Caption>
      </div>
      {issue && <IssueSummary issue={issue} onAction={onCommand} />}
      {stage !== null && <PipelineSteps stage={stage} pulse={state.pulse} caption={STAGE_CAPTION[state.key]} />}

      <CopyField label={D.detailSourceId} value={doc.source_object_id} onCopy={onCopy} />

      <section style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <SectionTitle>{D.currentRevision}</SectionTitle>
        <KeyValues rows={[
          [D.revisionId, rev?.source_revision_id ? shortId(rev.source_revision_id) : null, true],
          [D.lifecycle, optionLabel("lifecycle", rev?.lifecycle)],
          [D.observedAt, formatDate(rev?.observed_at, true)],
          [D.acceptedAt, rev?.accepted_at ? formatDate(rev.accepted_at, true) : null],
          [D.acceptedGeneration, rev?.accepted_generation, true],
          [D.admissionStatus, admissionLabel(rev?.admission_status)],
        ]} />
      </section>

      <section style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <SectionTitle>{D.processing}</SectionTitle>
        <KeyValues rows={[
          [D.processingState, optionLabel("processing_state", meta.processing_state)],
          [D.coverage, doc.coverage ? coverageLabel(doc.coverage) : null],
          [D.parser, doc.parser, true],
        ]} />
      </section>

      <section style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <SectionTitle>{D.projection}</SectionTitle>
        <KeyValues rows={[
          [D.publication, optionLabel("publication_status", doc.publication?.status)],
          [D.projectionHealth, healthLabel(doc.publication)],
          [D.freshness, doc.publication ? freshnessLabel(doc.publication.freshness) : null],
          [D.projectionKind, doc.publication?.projection_kind, true],
          [D.snapshot, doc.publication?.snapshot_id, true],
          [D.indexing, doc.publication?.indexed ? D.indexed : D.indexingNotReady],
        ]} />
      </section>

      <section style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <SectionTitle action={<StateMark tone={metaState.tone} label={metaState.label} />}>{D.accessMetadataTitle}</SectionTitle>
        <Caption style={{ lineHeight: 1.5 }}>{D.accessMetadataDescription}</Caption>
        <KeyValues rows={[
          [D.policyVersion, meta.access_policy ? `v${meta.access_policy.version}` : null],
          [D.policyGeneration, meta.access_policy?.generation, true],
          [D.securityDomainReference, meta.security_domain?.reference, true],
          [D.securityDomainVersion, meta.security_domain?.version, true],
        ]} />
      </section>
    </div>
  );
}

function History({ doc }) {
  const pal = usePal();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <Caption style={{ lineHeight: 1.5 }}>{D.protectedRevisionDetails}</Caption>
      <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column" }}>
        {doc.revisions.map((rev, i) => {
          const tomb = rev.lifecycle === "tombstoned";
          const last = i === doc.revisions.length - 1;
          return (
            <li key={rev.source_revision_id} style={{ display: "grid", gridTemplateColumns: "16px 1fr", gap: 10 }}>
              <span aria-hidden="true" style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                <span style={{
                  width: 9, height: 9, borderRadius: 5, marginTop: 5, flexShrink: 0,
                  background: i === 0 ? (tomb ? pal.danger : pal.accent) : pal.bgMuted,
                  border: `1px solid ${i === 0 ? "transparent" : pal.border}`,
                }} />
                {!last && <span style={{ flex: 1, width: 1, background: pal.borderSubtle, margin: "4px 0" }} />}
              </span>
              <div style={{ paddingBottom: last ? 0 : 16, minWidth: 0, display: "flex", flexDirection: "column", gap: 4 }}>
                <div style={{ display: "flex", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
                  <Text size="sm" weight="medium" style={{ fontFamily: tokens.font.mono }}>{shortId(rev.source_revision_id)}</Text>
                  <span style={{ ...tokens.type.xs, color: tomb ? pal.danger : i === 0 ? pal.text : pal.textTertiary, fontWeight: i === 0 ? tokens.weight.medium : tokens.weight.regular }}>
                    {i === 0 && !tomb ? D.current : optionLabel("lifecycle", rev.lifecycle)}
                  </span>
                </div>
                <Caption>{D.observedAt} {formatDate(rev.observed_at, true)}</Caption>
                {tomb && <Caption>{D.tombstoneRevision} · {reasonLabel(rev.reason)}</Caption>}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function Content({ doc }) {
  const pal = usePal();
  const readable = documentState(doc).key === "ready" && doc.current_revision && doc.segments?.length;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Caption style={{ lineHeight: 1.5 }}>{D.contentInspectionDescription}</Caption>
      {!readable ? <AlertBanner title={D.contentUnavailable} /> : doc.segments.map((seg, i) => (
        <div key={i} style={{ border: `1px solid ${pal.borderSubtle}`, borderRadius: tokens.radius.md, padding: 12, display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <Caption style={{ flex: 1 }}>{D.contentSegment} {i + 1} · {locatorLabel(seg.locator)}</Caption>
            <Caption style={{ fontFamily: tokens.font.mono, ...tokens.type.xs }}>{shortId(doc.current_revision.source_revision_id)}</Caption>
          </div>
          <Text size="sm" style={{ lineHeight: 1.55 }}>{seg.text}</Text>
        </div>
      ))}
    </div>
  );
}

// The issue summary already carries the primary command, so the footer keeps only the secondary ones.
function Actions({ doc, stacked, onReprocess, onReplace, onTombstone }) {
  const state = documentState(doc).key;
  if (state === "tombstoned") return <Caption style={{ lineHeight: 1.5 }}>{D.documentActions.tombstoneCreated}</Caption>;
  const busy = state === "parsing" || state === "indexing";
  const primary = documentIssue(doc)?.action;
  const showReplace = primary !== "replace";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {primary !== "reprocess" && state !== "rejected" && (
        <Button fullWidth size="sm" variant="outline" disabled={busy || state === "uploaded"}
          loading={busy} icon={<RotateCcw size={13} strokeWidth={1.5} />} onClick={onReprocess}>
          {D.commands.reprocess}
        </Button>
      )}
      <div style={{ display: "grid", gridTemplateColumns: stacked || !showReplace ? "1fr" : "1fr 1fr", gap: 8 }}>
        {showReplace && (
          <Button size="sm" variant="outline" disabled={busy} icon={<FileUp size={13} strokeWidth={1.5} />} onClick={onReplace}>
            {D.documentActions.submitReplacement}
          </Button>
        )}
        <Button size="sm" variant="ghost" disabled={busy} icon={<Trash2 size={13} strokeWidth={1.5} />} onClick={onTombstone}>
          {D.documentActions.submitTombstone}
        </Button>
      </div>
    </div>
  );
}

function Body({ doc, onCopy, onCommand }) {
  const [tab, setTab] = useState(TABS[0]);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <SubtleTabs tabs={TABS} value={tab} onChange={setTab} />
      <div role="tabpanel" aria-label={tab}>
        {tab === TABS[0] && <Overview doc={doc} onCopy={onCopy} onCommand={onCommand} />}
        {tab === TABS[1] && <History doc={doc} />}
        {tab === TABS[2] && <Content doc={doc} />}
      </div>
    </div>
  );
}

const commandHandler = ({ onReprocess, onReplace }) => (action) => (action === "replace" ? onReplace() : onReprocess());

export function SpineDetailsCard({ doc, onClose, onCopy, ...actions }) {
  return (
    <Card style={{ width: 400, flexShrink: 0, display: "flex", flexDirection: "column", overflow: "hidden", animation: `halaska-step-in ${motion.smooth} ${motion.emphasized} both` }}>
      <CardHeader title={detailsTitle(doc)} subtitle={D.overviewTitle}
        action={<IconButton size={30} label="Закрыть панель" icon={<X size={15} strokeWidth={1.25} />} onClick={onClose} />} />
      <div style={{ flex: 1, minHeight: 0, overflowY: "auto", margin: "0 -24px", padding: "0 24px 8px" }}>
        <Body doc={doc} onCopy={onCopy} onCommand={commandHandler(actions)} />
      </div>
      <Divider />
      <Actions doc={doc} {...actions} />
    </Card>
  );
}

export function SpineDetailsSheet({ doc, open, onClose, onCopy, ...actions }) {
  return (
    <Sheet open={open} onClose={onClose} title={detailsTitle(doc)}>
      <div style={{ display: "flex", flexDirection: "column", height: "calc(100vh - 92px)" }}>
        <div style={{ flex: 1, minHeight: 0, overflowY: "auto", margin: "0 -24px", padding: "0 24px 8px" }}>
          <Body doc={doc} onCopy={onCopy} onCommand={commandHandler(actions)} />
        </div>
        <Divider />
        <Actions doc={doc} stacked {...actions} />
      </div>
    </Sheet>
  );
}
