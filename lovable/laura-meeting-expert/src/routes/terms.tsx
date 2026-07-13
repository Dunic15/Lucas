import { createFileRoute } from "@tanstack/react-router";
import { LegalPage } from "@/components/legal/LegalPage";

export const Route = createFileRoute("/terms")({
  head: () => ({
    meta: [
      { title: "Terms of Service — Laura" },
      {
        name: "description",
        content:
          "Terms governing Laura accounts, meeting avatars, connected tools, free usage, and paid subscriptions.",
      },
    ],
  }),
  component: Terms,
});

function Terms() {
  return (
    <LegalPage
      title="Terms of Service"
      effectiveDate="July 13, 2026"
      intro="These terms govern access to Laura, a business AI meeting-avatar service operated by SFF Studio. By creating an account or using the service, you agree to them."
    >
      <section>
        <h2>1. Eligibility and authority</h2>
        <p>
          You must be at least 18 and legally able to enter this agreement. If you use Laura for an
          organization, you confirm that you are authorized to accept these terms and connect the
          organization&apos;s meetings, data, and workspace services.
        </p>
      </section>

      <section>
        <h2>2. Accounts and security</h2>
        <p>
          You must provide accurate account information, protect access to your Google account and
          connected services, and notify us promptly of unauthorized access. You are responsible
          for activity performed through your account and for managing access granted to members of
          your organization.
        </p>
      </section>

      <section>
        <h2>3. Meeting notice and permissions</h2>
        <p>
          Laura can join meetings, process speech and transcripts, and create summaries and actions.
          You are responsible for telling participants that an AI avatar is present and obtaining
          any consent required by the meeting platform, your organization, or applicable law before
          inviting or activating Laura.
        </p>
      </section>

      <section>
        <h2>4. Free and paid plans</h2>
        <ul>
          <li>
            A new organization receives 15 lifetime avatar-minutes shared across Laura and Cedric.
            No payment card is required for that free allocation.
          </li>
          <li>
            The Solo plan is €49 per monthly billing period and includes 300 shared avatar-minutes
            per billing period, unless the checkout page displays a different tax-inclusive total
            or a later offer you accept.
          </li>
          <li>
            Usage is measured from the avatar&apos;s meeting join until leave or terminal end.
            Attempts to start a new meeting can be blocked when no entitlement remains.
          </li>
          <li>
            Unused included minutes do not roll over unless the product or a written agreement
            expressly says otherwise. Additional use requires another available plan or written
            agreement; there is no automatic overage charge unless you explicitly accept one.
          </li>
        </ul>
      </section>

      <section>
        <h2>5. Billing, renewal, and cancellation</h2>
        <p>
          Stripe processes payments. Paid subscriptions renew automatically each billing period
          until canceled. Prices exclude taxes unless Checkout states otherwise. You may update
          payment details or cancel through the Customer Portal. Cancellation normally takes effect
          at the end of the current paid period; fees already paid are non-refundable except where
          required by law or expressly stated during purchase.
        </p>
      </section>

      <section>
        <h2>6. Connected services</h2>
        <p>
          Workspace connections are optional. When you connect Google, Slack, or another provider,
          you authorize Laura to access the selected data and perform the actions shown in the
          product. Your use also remains subject to that provider&apos;s terms. You can disconnect a
          service, but actions already completed in the external service may remain there.
        </p>
      </section>

      <section>
        <h2>7. Customer content</h2>
        <p>
          You retain ownership of content you provide. You grant us a limited right to host,
          transmit, transform, and process that content only as needed to provide, secure, support,
          and improve the contracted service. You confirm that you have the rights and permissions
          needed to provide meeting and workspace content to Laura.
        </p>
      </section>

      <section>
        <h2>8. Acceptable use</h2>
        <p>You must not use Laura to:</p>
        <ul>
          <li>break the law, violate privacy rights, or process content without required consent;</li>
          <li>mislead people about recording, monitoring, identity, or an avatar&apos;s capabilities;</li>
          <li>attempt to access another organization&apos;s data or defeat security and usage controls;</li>
          <li>introduce malware, overload the service, scrape it, or reverse engineer protected systems;</li>
          <li>generate or execute harmful, fraudulent, abusive, or infringing activity.</li>
        </ul>
      </section>

      <section>
        <h2>9. AI outputs and human review</h2>
        <p>
          AI-generated speech, summaries, citations, decisions, and actions can be incomplete or
          wrong. Laura is an assistance product, not a substitute for professional legal, medical,
          financial, employment, safety, or compliance advice. You are responsible for reviewing
          important outputs and actions before relying on them.
        </p>
      </section>

      <section>
        <h2>10. Availability and changes</h2>
        <p>
          We work to keep Laura available and secure, but meetings and connected actions depend on
          third-party platforms and network services. We do not promise uninterrupted operation.
          We may change or discontinue features, and will provide reasonable notice when a material
          change adversely affects an active paid subscription.
        </p>
      </section>

      <section>
        <h2>11. Suspension and termination</h2>
        <p>
          You may stop using Laura at any time. We may suspend or terminate access for a serious
          security risk, non-payment, unlawful use, material breach, or conduct that threatens the
          service or other customers. Where practical, we will give notice and an opportunity to
          cure.
        </p>
      </section>

      <section>
        <h2>12. Disclaimers and liability</h2>
        <p>
          To the maximum extent permitted by law, the service is provided without implied
          warranties and neither party is liable for indirect, incidental, special, punitive, or
          consequential loss. Nothing in these terms excludes liability that cannot legally be
          excluded or limits mandatory consumer rights.
        </p>
      </section>

      <section>
        <h2>13. Governing terms and updates</h2>
        <p>
          An order form or enterprise agreement controls if it conflicts with these online terms.
          Otherwise, the law and courts applicable to SFF Studio&apos;s registered jurisdiction
          govern, subject to mandatory protections that apply to you. We may update these terms
          prospectively and will provide notice when required.
        </p>
      </section>

      <section>
        <h2>14. Contact</h2>
        <p>
          Questions about these terms or billing can be sent to{" "}
          <a href="mailto:duccio@sffstudio.com">duccio@sffstudio.com</a>.
        </p>
      </section>
    </LegalPage>
  );
}
