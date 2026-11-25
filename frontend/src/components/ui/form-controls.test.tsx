import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Textarea } from "./form-controls";

function NoteForm({ onSubmit }: { onSubmit: () => void }) {
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        onSubmit();
      }}
    >
      <Textarea aria-label="Note" />
    </form>
  );
}

describe("Textarea", () => {
  it("submits the form on Ctrl+Enter", async () => {
    const onSubmit = vi.fn();
    render(<NoteForm onSubmit={onSubmit} />);
    await userEvent.type(screen.getByLabelText("Note"), "checked{Control>}{Enter}{/Control}");
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("keeps Enter alone as a newline", async () => {
    const onSubmit = vi.fn();
    render(<NoteForm onSubmit={onSubmit} />);
    const note = screen.getByLabelText("Note");
    await userEvent.type(note, "line one{Enter}line two");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(note).toHaveValue("line one\nline two");
  });
});
