import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";

export function LegalPage({
  title,
  effectiveDate,
  intro,
  children,
}: {
  title: string;
  effectiveDate: string;
  intro: string;
  children: ReactNode;
}) {
  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border bg-background/95 px-6 py-4 sm:px-8">
        <div className="mx-auto flex w-full max-w-4xl items-center justify-between">
          <Link to="/" className="flex items-center gap-2.5" aria-label="Laura home">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-primary text-sm font-bold text-primary-foreground shadow-primary">
              L
            </span>
            <span className="text-lg font-semibold tracking-tight">Laura</span>
          </Link>
          <Link
            to="/"
            className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            Back to home
          </Link>
        </div>
      </header>

      <main className="px-6 py-14 sm:px-8 sm:py-20">
        <article className="mx-auto w-full max-w-4xl">
          <p className="text-sm font-semibold uppercase tracking-[0.18em] text-primary">
            Legal
          </p>
          <h1 className="mt-3 text-4xl font-semibold tracking-tight sm:text-5xl">{title}</h1>
          <p className="mt-4 text-sm text-muted-foreground">Effective {effectiveDate}</p>
          <p className="mt-8 max-w-3xl text-lg leading-8 text-muted-foreground">{intro}</p>
          <div className="mt-12 space-y-10 [&_a]:font-medium [&_a]:text-primary [&_a]:underline-offset-4 hover:[&_a]:underline [&_h2]:text-2xl [&_h2]:font-semibold [&_h2]:tracking-tight [&_li]:leading-7 [&_p]:leading-7 [&_p]:text-muted-foreground [&_ul]:list-disc [&_ul]:space-y-2 [&_ul]:pl-6">
            {children}
          </div>
        </article>
      </main>

      <footer className="border-t border-border px-6 py-8 sm:px-8">
        <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <p>© {new Date().getFullYear()} Laura. All rights reserved.</p>
          <nav className="flex gap-5" aria-label="Legal">
            <Link to="/privacy" className="hover:text-foreground">
              Privacy
            </Link>
            <Link to="/terms" className="hover:text-foreground">
              Terms
            </Link>
            <a href="mailto:duccio@sffstudio.com" className="hover:text-foreground">
              Contact
            </a>
          </nav>
        </div>
      </footer>
    </div>
  );
}
