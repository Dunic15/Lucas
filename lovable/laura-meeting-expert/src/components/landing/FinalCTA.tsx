import { ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DEMO_LINK, Reveal } from "./primitives";

export function FinalCTA() {
  return (
    <section className="px-6 py-24 sm:px-8 md:py-32">
      <div className="mx-auto w-full max-w-5xl">
        <Reveal>
          <div className="relative overflow-hidden rounded-[2rem] border border-border bg-card px-8 py-16 text-center shadow-elevated sm:px-16">
            <div className="pointer-events-none absolute -top-24 left-1/2 h-64 w-[40rem] -translate-x-1/2 rounded-full bg-gradient-primary opacity-15 blur-3xl" />
            <h2 className="relative text-3xl font-semibold tracking-tight sm:text-4xl md:text-5xl">
              Bring Laura into your next meeting.
            </h2>
            <p className="relative mx-auto mt-5 max-w-xl text-lg text-muted-foreground">
              See how a live AI process expert keeps your team aligned, grounded, and one step
              ahead.
            </p>
            <div className="relative mt-9 flex justify-center">
              <Button asChild variant="hero" size="xl">
                <a href={DEMO_LINK}>
                  Book a demo
                  <ArrowRight />
                </a>
              </Button>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

const footerLinks = [
  { label: "How it works", href: "#solution" },
  { label: "Product", href: "#product" },
  { label: "Use Cases", href: "#use-cases" },
  { label: "Contact", href: DEMO_LINK },
];

export function Footer() {
  return (
    <footer className="border-t border-border bg-gradient-soft px-6 py-14 sm:px-8">
      <div className="mx-auto flex w-full max-w-6xl flex-col items-start justify-between gap-8 md:flex-row md:items-center">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-primary text-sm font-bold text-primary-foreground shadow-primary">
              L
            </span>
            <span className="text-lg font-semibold tracking-tight">Laura</span>
          </div>
          <p className="mt-3 max-w-xs text-sm text-muted-foreground">
            Live AI process experts for every meeting.
          </p>
        </div>

        <nav className="flex flex-wrap gap-x-8 gap-y-3">
          {footerLinks.map((link) => (
            <a
              key={link.label}
              href={link.href}
              className="text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
            >
              {link.label}
            </a>
          ))}
        </nav>
      </div>

      <div className="mx-auto mt-10 w-full max-w-6xl border-t border-border pt-6">
        <p className="text-xs text-muted-foreground">
          © {new Date().getFullYear()} Laura. All rights reserved.
        </p>
      </div>
    </footer>
  );
}
