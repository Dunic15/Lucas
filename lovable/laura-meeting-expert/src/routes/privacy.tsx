import { createFileRoute } from "@tanstack/react-router";
import { LegalPage } from "@/components/legal/LegalPage";

export const Route = createFileRoute("/privacy")({
  head: () => ({
    meta: [
      { title: "Privacy Policy — Laura" },
      {
        name: "description",
        content:
          "How Laura collects, uses, protects, and deletes account, meeting, billing, and connected-workspace data.",
      },
    ],
  }),
  component: Privacy,
});

function Privacy() {
  return (
    <LegalPage
      title="Privacy Policy"
      effectiveDate="July 13, 2026"
      intro="Laura is operated by SFF Studio. This policy explains what we process when you use the Laura website, dashboard, meeting avatars, billing, and optional workspace connections."
    >
      <section>
        <h2>1. Who controls your data</h2>
        <p>
          SFF Studio operates Laura and is the controller for account and commercial data unless
          an enterprise agreement states otherwise. For privacy questions, access requests, or
          deletion requests, email{" "}
          <a href="mailto:duccio@sffstudio.com">duccio@sffstudio.com</a>.
        </p>
      </section>

      <section>
        <h2>2. Data we process</h2>
        <ul>
          <li>
            <strong>Account data:</strong> your Google account identifier, verified email address,
            name, profile image, organization, authentication events, and account preferences.
          </li>
          <li>
            <strong>Meeting data:</strong> meeting links and identifiers, participant and speaker
            information, live transcript text, timestamps, avatar activity, summaries, decisions,
            action items, and other outputs created during or after a meeting.
          </li>
          <li>
            <strong>Connected-workspace data:</strong> only after you connect a service, the
            configuration and content needed to perform the action you request through services
            such as Slack, Google Calendar, Gmail, or Google Drive.
          </li>
          <li>
            <strong>Billing data:</strong> plan, entitlement, usage, subscription status, and
            Stripe customer or subscription identifiers. Stripe handles payment-card details.
          </li>
          <li>
            <strong>Technical data:</strong> IP address, browser and device information, security
            events, service logs, diagnostics, and cookie/session identifiers.
          </li>
        </ul>
      </section>

      <section>
        <h2>3. Why we process it</h2>
        <p>
          We process data to create and secure your account, place the avatar in meetings, provide
          grounded answers, generate meeting outputs, connect tools at your direction, enforce
          usage limits, bill subscriptions, support customers, prevent abuse, and improve service
          reliability. We rely on performance of our contract, legitimate interests in operating a
          secure product, consent where required, and compliance with legal obligations.
        </p>
      </section>

      <section>
        <h2>4. Google and connected-service data</h2>
        <p>
          Laura initially requests only Google OpenID scopes for sign-in: identity, email, and
          profile. Calendar, Gmail, Drive, Slack, or other workspace access is authorized separately
          through Cedric when you choose to connect that service. Laura does not ask you to
          reconnect the same tools. We request the narrowest scopes needed for the feature you activate.
        </p>
        <p>
          We use connected-service data only to provide or improve the user-facing features you
          request. We do not sell it, use it for advertising, or use Google Workspace API data to
          train generalized AI models. Disconnecting a service stops new access; you may also ask
          us to delete previously stored connected-service data.
        </p>
      </section>

      <section>
        <h2>5. AI and service providers</h2>
        <p>
          Laura uses contracted infrastructure and subprocessors to host the service, join
          meetings, transcribe speech, generate AI responses, send requested actions, and process
          payments. These may include cloud hosting and database providers, meeting and
          transcription providers, AI model providers, connected workspace providers, and Stripe.
          They process data under their own security commitments and only for the service function
          we engage them to perform.
        </p>
      </section>

      <section>
        <h2>6. Sharing</h2>
        <p>
          We share data with your organization and authorized workspace members according to your
          account permissions; with providers needed to operate Laura; when you direct an action to
          a connected service; during a corporate transaction subject to appropriate safeguards;
          or when legally required. We do not sell personal data.
        </p>
      </section>

      <section>
        <h2>7. Retention and deletion</h2>
        <p>
          Product data is retained only as long as needed to provide the service, meet the
          retention setting or contract that applies to your organization, resolve disputes, and
          satisfy legal obligations. Account, security, and billing records may be kept longer when
          required for fraud prevention, accounting, or law. We will publish a specific default
          retention period when the corresponding automated deletion control is enabled.
        </p>
        <p>
          You can request account or data deletion by emailing us. We may retain limited records
          that we are legally required to keep and will explain any exception that applies.
        </p>
      </section>

      <section>
        <h2>8. Security and tenant isolation</h2>
        <p>
          We use access controls, encrypted transport, secret management, tenant-scoped database
          controls, audit and monitoring measures, and restricted operational access. No system is
          perfectly secure, so please contact us immediately if you believe your account or
          workspace connection has been compromised.
        </p>
      </section>

      <section>
        <h2>9. Your choices and rights</h2>
        <p>
          Depending on your location, you may have rights to access, correct, export, restrict,
          object to, or delete personal data, and to complain to a data-protection authority. You
          may disconnect workspace services and cancel a paid plan through the product or Stripe
          Customer Portal. We may need to verify your identity before completing a request.
        </p>
      </section>

      <section>
        <h2>10. Cookies</h2>
        <p>
          Laura uses essential cookies to bind the Google sign-in flow, maintain your authenticated
          session, and protect against request forgery. We do not require advertising cookies to
          operate the product.
        </p>
      </section>

      <section>
        <h2>11. International processing and children</h2>
        <p>
          Providers may process data in countries other than your own. Where required, we use
          contractual and legal safeguards for international transfers. Laura is a business
          product and is not directed to children under 16.
        </p>
      </section>

      <section>
        <h2>12. Changes</h2>
        <p>
          We may update this policy as the product or law changes. We will update the effective
          date and provide additional notice when a material change requires it.
        </p>
      </section>
    </LegalPage>
  );
}
