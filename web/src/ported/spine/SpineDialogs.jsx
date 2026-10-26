import { useRef, useState } from "react";
import { FileText, Upload, X } from "lucide-react";
import { AlertBanner, Button, Caption, IconButton, Label, RadioGroup, Text, motion, tokens, usePal } from "../halaska-kit";
import { SpineModal } from "./SpineParts";
import { SAMPLE_FILES, TOMBSTONE_REASONS, UPLOAD_ACCEPT, formatSize, validateBatch } from "./spineData";
import { ru } from "./ru";

const U = ru.documents.upload;
const A = ru.documents.documentActions;

function Dropzone({ multiple, onFiles, onSample }) {
  const pal = usePal();
  const inputRef = useRef(null);
  const [over, setOver] = useState(false);
  const pick = (list) => onFiles(Array.from(list ?? []));
  return (
    <div
      aria-label={U.dropzone}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); pick(multiple ? e.dataTransfer.files : [e.dataTransfer.files[0]].filter(Boolean)); }}
      style={{
        border: `1px dashed ${over ? pal.accent : pal.border}`, borderRadius: tokens.radius.md,
        background: over ? pal.accentBg : pal.bgSubtle, padding: "20px 16px",
        display: "flex", flexDirection: "column", alignItems: "center", gap: 8, textAlign: "center",
        transition: `all ${motion.normal} ${motion.easeInOut}`,
      }}>
      <Upload size={18} strokeWidth={1.25} color={pal.textSecondary} />
      <Text size="sm" weight="medium">{U.dropzone}</Text>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Caption>{U.or}</Caption>
        <Button type="button" size="sm" variant="outline" onClick={() => inputRef.current?.click()}>{U.chooseFiles}</Button>
        {onSample && <Button type="button" size="sm" variant="ghost" onClick={onSample}>Пример</Button>}
      </div>
      <input ref={inputRef} type="file" hidden multiple={multiple} accept={UPLOAD_ACCEPT}
        onChange={(e) => { pick(e.target.files); e.target.value = ""; }} />
    </div>
  );
}

function Queue({ files, onRemove }) {
  const pal = usePal();
  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 4, maxHeight: 180, overflowY: "auto" }}>
      {files.map((f, i) => (
        <li key={`${f.name}-${i}`} style={{
          display: "flex", alignItems: "center", gap: 10, padding: "6px 6px 6px 10px",
          borderRadius: tokens.radius.sm, border: `1px solid ${pal.borderSubtle}`,
        }}>
          <FileText size={14} strokeWidth={1.25} color={pal.textSecondary} style={{ flexShrink: 0 }} />
          <Text size="sm" truncate style={{ flex: 1, minWidth: 0 }}>{f.name}</Text>
          <Caption style={{ fontFamily: tokens.font.mono, ...tokens.type.xs }}>{formatSize(f.size)}</Caption>
          <IconButton size={24} label={`Убрать ${f.name}`} icon={<X size={12} strokeWidth={1.5} />} onClick={() => onRemove(i)} />
        </li>
      ))}
    </ul>
  );
}

export function UploadDialog({ open, onClose, onSubmit }) {
  const [files, setFiles] = useState([]);
  const error = files.length ? validateBatch(files) : null;
  const close = () => { setFiles([]); onClose(); };
  return (
    <SpineModal open={open} onClose={close} title={U.title} description={U.description} width={520}
      submitLabel={U.submit} submitDisabled={!files.length || !!error} onSubmit={() => onSubmit(files)}>
      <Dropzone multiple onFiles={(next) => setFiles(f => [...f, ...next])} />
      <Caption>{U.acceptedTypes}</Caption>
      {files.length > 0 && (
        <div role="region" aria-label={U.review} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
            <Label>{U.review}</Label>
            <Caption>{U.selectedCount.replace("{count}", String(files.length))}</Caption>
          </div>
          <Queue files={files} onRemove={(i) => setFiles(f => f.filter((_, j) => j !== i))} />
        </div>
      )}
      {error && <AlertBanner variant="danger" title={error} />}
    </SpineModal>
  );
}

export function ReplaceDialog({ open, onClose, onSubmit }) {
  const [file, setFile] = useState(null);
  const error = file ? validateBatch([file]) : null;
  const close = () => { setFile(null); onClose(); };
  return (
    <SpineModal open={open} onClose={close} title={A.replacementTitle} description={A.replacementDescription}
      submitLabel={A.submitReplacement} submitDisabled={!file || !!error} onSubmit={() => onSubmit(file)}>
      <Label>{A.replacementFile}</Label>
      {file
        ? <Queue files={[file]} onRemove={() => setFile(null)} />
        : <Dropzone onFiles={(list) => setFile(list[0] ?? null)} />}
      {error && <AlertBanner variant="danger" title={error} />}
    </SpineModal>
  );
}

export function TombstoneDialog({ open, onClose, onSubmit }) {
  const [reason, setReason] = useState(TOMBSTONE_REASONS[0].value);
  return (
    <SpineModal open={open} onClose={onClose} title={A.tombstoneTitle} description={A.tombstoneDescription}
      submitLabel={A.submitTombstone} danger onSubmit={() => onSubmit(reason)}>
      <RadioGroup label={A.deletionReason} options={TOMBSTONE_REASONS} value={reason} onChange={setReason} />
      <Caption style={{ lineHeight: 1.5 }}>{A.description}</Caption>
    </SpineModal>
  );
}
