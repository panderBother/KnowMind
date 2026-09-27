import { useCallback, useRef, useState } from "react";
import { FileUp, UploadCloud, X } from "lucide-react";

import { filterSupportedFiles } from "@/services/documents";

type Props = {
  kbName: string;
  disabled?: boolean;
  uploading: boolean;
  uploadProgress: number;
  versionTarget?: { filename: string } | null;
  onSelectFiles: (files: File[]) => void | Promise<void>;
  onBrowseClick: () => void;
  onCancelVersion?: () => void;
};

export function DocumentUploadZone({
  kbName,
  disabled,
  uploading,
  uploadProgress,
  versionTarget,
  onSelectFiles,
  onBrowseClick,
  onCancelVersion,
}: Props) {
  const [dragOver, setDragOver] = useState(false);
  const dragDepth = useRef(0);

  const handleFiles = useCallback(
    (files: FileList | File[] | null) => {
      if (!files?.length || disabled || uploading) return;
      const arr = filterSupportedFiles(files);
      if (arr.length) void onSelectFiles(arr);
    },
    [disabled, onSelectFiles, uploading],
  );

  const onDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragDepth.current += 1;
    setDragOver(true);
  };

  const onDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setDragOver(false);
  };

  const onDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = disabled || uploading ? "none" : "copy";
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragDepth.current = 0;
    setDragOver(false);
    handleFiles(e.dataTransfer.files);
  };

  const zoneCls = [
    "relative rounded-2xl border-2 border-dashed p-6 text-center transition-colors lg:rounded-xl lg:p-10",
    dragOver && !disabled && !uploading
      ? "border-primary bg-primary-soft/40"
      : versionTarget
        ? "border-primary/50 bg-primary-soft/20"
      : "border-slate-200 bg-white",
    disabled ? "opacity-50" : "",
  ].join(" ");

  return (
    <div
      role="button"
      tabIndex={0}
      className={zoneCls}
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDragOver={onDragOver}
      onDrop={onDrop}
      aria-label={versionTarget ? `更新文档 ${versionTarget.filename}` : `上传文档到 ${kbName}`}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onBrowseClick();
        }
      }}
    >
      {versionTarget && onCancelVersion ? (
        <button
          type="button"
          disabled={uploading}
          onClick={(event) => {
            event.stopPropagation();
            onCancelVersion();
          }}
          className="absolute right-3 top-3 inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs font-medium text-slate-500 hover:bg-white hover:text-slate-800 disabled:opacity-50"
          aria-label="取消更新版本"
        >
          <X className="h-3.5 w-3.5" />
          取消更新
        </button>
      ) : null}

      {versionTarget ? (
        <FileUp className="mx-auto mb-2 h-9 w-9 text-primary lg:h-10 lg:w-10" />
      ) : (
        <UploadCloud className="mx-auto mb-2 h-9 w-9 text-primary lg:h-10 lg:w-10" />
      )}
      {versionTarget ? (
        <>
          <p className="text-sm font-semibold text-slate-900">更新「{versionTarget.filename}」</p>
          <p className="mt-1 text-xs text-slate-500">
            {dragOver ? "松开鼠标即可上传这个新版本" : "请选择一个新文件，文件名可以与当前文档不同"}
          </p>
          <p className="mt-1 text-xs text-slate-400">处理完成前，当前版本仍可正常检索</p>
        </>
      ) : (
        <p className="text-sm text-slate-600">
          {dragOver ? "松开鼠标即可上传" : `拖拽文件到此处，或选择上传至「${kbName}」`}
        </p>
      )}
      <p className="mt-1 text-xs text-slate-400">
        支持 PDF、DOCX、Excel、CSV、Markdown、TXT、图片
      </p>

      {uploading && !versionTarget ? (
        <div className="mx-auto mt-4 w-full max-w-xs">
          <div className="flex items-center justify-between text-xs text-slate-500">
            <span>上传进度</span>
            <span>{uploadProgress}%</span>
          </div>
          <div className="mt-1.5 h-2 overflow-hidden rounded-full bg-slate-100">
            <div
              className="h-full rounded-full bg-primary transition-all duration-200"
              style={{ width: `${uploadProgress}%` }}
            />
          </div>
        </div>
      ) : null}

      <button
        type="button"
        disabled={disabled || uploading}
        onClick={onBrowseClick}
        className="mt-3 w-full max-w-xs rounded-xl bg-primary py-2.5 text-sm font-semibold text-white hover:bg-primary-hover disabled:opacity-50 lg:mt-4 lg:w-auto lg:px-6"
      >
        {uploading
          ? versionTarget
            ? "正在提交新版本…"
            : `上传中 ${uploadProgress}%`
          : versionTarget
            ? "选择新版本文件"
            : "选择文件"}
      </button>
    </div>
  );
}
