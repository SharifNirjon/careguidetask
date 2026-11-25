"use client";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogFooter } from "@/components/ui/dialog";

/**
 * 412: someone saved this invoice after it was loaded. Either take their
 * version (discarding mine), or reload theirs and re-apply my edits on top, to
 * review and save again.
 */
export function ConflictDialog({
  open,
  onReload,
  onKeepMine,
  onCancel,
}: {
  open: boolean;
  onReload: () => void;
  onKeepMine: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          onCancel();
        }
      }}
    >
      <DialogContent
        title="This invoice changed while you were editing"
        description="Someone else saved it after you opened it, so your save was not applied."
      >
        <ul className="list-disc space-y-1 pl-5 text-sm">
          <li>
            <strong>Reload</strong> shows their version and discards your edits.
          </li>
          <li>
            <strong>Keep my changes</strong> loads their version, then re-applies your edits on top so you can
            check and save again.
          </li>
        </ul>
        <DialogFooter>
          <Button variant="outline" onClick={onReload}>
            Reload
          </Button>
          <Button onClick={onKeepMine}>Keep my changes</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
