"use client";

import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Kbd } from "@/components/ui/misc";

import { SHORTCUTS } from "../use-shortcuts";

export function ShortcutsHelp({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title="Keyboard shortcuts"
        description="Letter shortcuts are off while you type in a field."
      >
        <table className="w-full text-sm">
          <caption className="sr-only">Keyboard shortcuts</caption>
          <tbody>
            {SHORTCUTS.map(({ keys, action }) => (
              <tr key={keys} className="border-border border-b">
                <th scope="row" className="py-2 pr-4 text-left font-normal">
                  <Kbd>{keys}</Kbd>
                </th>
                <td className="py-2">{action}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </DialogContent>
    </Dialog>
  );
}
