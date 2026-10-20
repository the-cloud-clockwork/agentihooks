---
title: Ledger TLS
parent: Reference
nav_order: 28
---

# Ledger TLS
{: .no_toc }

A ledger credential never crosses a network in clear text. Every ledger client refuses to send a credential over
plain http to a host that is not loopback, and the ledger server serves https from a configured certificate and key.

1. TOC
{:toc}

---

## The refusal

`ledger_link.urlopen` is the one opener for every request that carries a ledger credential: the remote ledger
client (`ResourceClient`), the ledger CLI, plan reads, proof ledger calls and controls, uploads and the event
stream. Before it opens a connection it checks the request.

| Request | Result |
|---|---|
| `https://` to any host | Sent. |
| `http://` with no credential header | Sent. |
| `http://` with a credential header to a host whose addresses are all loopback | Sent. |
| `http://` with a credential header to any other host | Refused before the connection opens. |

The credential headers are `X-Controller-Credential`, `X-Hive-Credential`, `X-Ledger-Token`, `X-Proof-Credential`,
`X-Ledger-Tick` and `X-Ledger-Migration`, matched without regard to case. A header with an empty value does not count.

The client resolves the host name and treats it as loopback only when every address it resolves to is a loopback
address. `127.0.0.1`, `::1` and `localhost` pass. A name that does not resolve, or one that also resolves to a
private or public address, counts as off loopback. `hive join` has its own rule: it refuses any hive URL that is not
`https://` off loopback, whether or not the request carries a credential.

A refused request stops the command with:

```text
a ledger credential off loopback travels only over https, not to http://swarm.example.internal:8765: serve the ledger with LEDGER_TLS_CERT and LEDGER_TLS_KEY and address it as https
```

Nothing reaches the network, so the credential stays off the wire. Retrying does not help: serve the ledger over
TLS and point the client at its https address.

## Serving the ledger over TLS

The ledger server serves https when both settings are set:

| Variable | Meaning |
|---|---|
| `LEDGER_TLS_CERT` | Path to the PEM certificate, with any intermediate certificates after it. |
| `LEDGER_TLS_KEY` | Path to the PEM private key for that certificate. |

- Set both or neither. One without the other stops the server and every client with `the ledger serves TLS only
  with both LEDGER_TLS_CERT and LEDGER_TLS_KEY`.
- With both set, the local base address becomes `https://<LEDGER_HOST>:<LEDGER_PORT>`, and the page accepts
  `https://` origins for its own address and loopback.
- During the TLS handshake each socket read or write times out after ten seconds, so a client that stalls is
  dropped. A failed handshake, such as a plain http client on the TLS port, is dropped without an error log entry;
  other request errors are still logged.
- Hosts listed in `SWARM_ALLOWED_HOSTS` are accepted as page origins under both `http://` and `https://`, whatever
  the TLS setting.

The certificate must name every host clients use to reach the ledger as a subject alternative name. A client
that reaches it on loopback through `https://127.0.0.1` needs `IP:127.0.0.1` in the certificate too.

Example CA and certificate for a ledger reached as `swarm.example.internal`:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=swarm-ledger-ca" \
  -keyout ca.key -out ca.pem
openssl req -newkey rsa:2048 -nodes -subj "/CN=swarm.example.internal" \
  -keyout key.pem -out ledger.csr
openssl x509 -req -in ledger.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 365 \
  -extfile <(printf 'subjectAltName=DNS:swarm.example.internal,DNS:localhost,IP:127.0.0.1') \
  -out cert.pem
```

## Trusting the ledger certificate

An https client verifies the ledger certificate. It picks its trust in this order:

| Variable | Meaning |
|---|---|
| `LEDGER_CA_PEM` | The PEM text of the CA that signed the ledger certificate. Set, it is the only CA an https ledger request trusts. |
| `LEDGER_CA_FILE` | Without `LEDGER_CA_PEM`, a CA file trusted beside the system roots. Defaults to `ledger-ca.pem` in the agentihooks home. A set path that names no file, or a file without a valid certificate, stops the client. |

`agentihooks init` writes `ledger-ca.pem` in the agentihooks home from OpenBao `k8s/agentihooks-swarm-ledger-ca`,
field `ca.crt`, when `OPENBAO_URL` and `OPENBAO_TOKEN` are set. With neither variable set and no `ledger-ca.pem`,
the client trusts the system roots only, which suits a ledger behind a publicly signed certificate. Every sender
listed in [The refusal](#the-refusal) and the ledger health probe use this trust.

## Workstation ledger in Docker

`agentihooks ledger serve --ensure` runs the shared ledger folder (`~/development-ledger`) in Docker through
`deploy/compose/ledger.yaml` when `docker` is on the path, unless `LEDGER_SERVER_RUNTIME=process`; otherwise it
runs the server as a host process. Its compose file passes `LEDGER_TLS_CERT` and `LEDGER_TLS_KEY` from the
environment into the container and mounts `~/.agentihooks`, so keep the files there. An adopted ledger runs through
`deploy/compose/adopt.yaml`, which passes the same two settings but mounts `$SWARM_ADOPT_HOME/.agentihooks` as its
home; for it, read `~/.agentihooks` below as that folder. Neither compose file passes `LEDGER_CA_FILE` or
`LEDGER_CA_PEM`, so put the CA at the default `~/.agentihooks/ledger-ca.pem`, where the container health check
finds it:

```bash
mkdir -p ~/.agentihooks/ledger-tls
cp cert.pem key.pem ~/.agentihooks/ledger-tls/
cp ca.pem ~/.agentihooks/ledger-ca.pem
export LEDGER_TLS_CERT=$HOME/.agentihooks/ledger-tls/cert.pem
export LEDGER_TLS_KEY=$HOME/.agentihooks/ledger-tls/key.pem
agentihooks ledger serve --ensure
```

The health check reaches the ledger at `https://127.0.0.1`, so the certificate needs `IP:127.0.0.1`. Three failed
checks in a row end the server.

That ledger listens on `127.0.0.1`, so its own clients already pass the loopback rule; TLS here matters once
another machine reaches it.

## Helm chart

Set `ledger.tls.secretName` to a Secret in the release namespace with three keys:

| Key | Content |
|---|---|
| `tls.crt` | The ledger certificate. It must name `<fullname>-ledger`, the service and ingress backend name, as a subject alternative name. |
| `tls.key` | Its private key. |
| `ca.crt` | The CA that signed it. |

```bash
kubectl create secret generic swarm-ledger-tls --namespace swarm \
  --from-file=tls.crt=cert.pem \
  --from-file=tls.key=key.pem \
  --from-file=ca.crt=ca.pem
helm upgrade --install swarm deploy/helm/agentihooks-swarm --namespace swarm \
  --set ledger.tls.secretName=swarm-ledger-tls
```

With the setting the chart:

- mounts `tls.crt` and `tls.key` into the ledger at `/run/ledger-tls` and sets `LEDGER_TLS_CERT` and `LEDGER_TLS_KEY`;
- makes the in-cluster `LEDGER_URL` `https://<fullname>-ledger:<port>`;
- gives every pod that talks to the ledger `LEDGER_CA_PEM` from `ca.crt`;
- runs the ledger probes and the `helm test` health check over https;
- when `ingress.enabled` is true, annotates the ingress so nginx speaks https to the ledger and verifies its
  certificate against `ca.crt` with the name `<fullname>-ledger`;
- marks the ledger StatefulSet for a restart by Reloader when the Secret changes, so a renewed certificate takes
  effect.

Left empty, the default, the ledger serves plain http inside the cluster. Any pod that sends a credential to it
then sends it to a non loopback service address, and the client refuses it.

The kind smoke test (`deploy/helm/agentihooks-swarm/ci/kind-smoke.sh`) renders the chart with the setting and
checks the ingress and Reloader annotations, then installs it with a generated certificate and runs
`helm install --wait` and `helm test`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `a ledger credential off loopback travels only over https` | A client points at an `http://` ledger address on another host. | Serve the ledger with `LEDGER_TLS_CERT` and `LEDGER_TLS_KEY` and set `SWARM_URL` or `LEDGER_URL` to its `https://` address. |
| The same refusal with a host you expect to be local | The name resolves to at least one address that is not loopback. | Use `127.0.0.1` or `localhost`, or switch to https. |
| `the ledger serves TLS only with both LEDGER_TLS_CERT and LEDGER_TLS_KEY` | Only one of the two is set. | Set both, or unset both. |
| `certificate verify failed` from a client | The client does not trust the ledger's CA, or the certificate lacks the host name. | Set `LEDGER_CA_PEM` or `LEDGER_CA_FILE` to the signing CA; reissue the certificate with the right subject alternative names. |
| `LEDGER_CA_FILE names no file` | `LEDGER_CA_FILE` points at a missing path. | Fix the path or unset it. |
| A browser or curl on `http://` gets no answer from a TLS ledger | The port speaks only TLS; the server drops plain http silently. | Use `https://`. |
| The ingress answers 502 after enabling TLS | nginx cannot verify the backend certificate. | Make sure `tls.crt` names `<fullname>-ledger` and `ca.crt` signed it. |

## Limits

- The refusal covers requests sent through `ledger_link.urlopen`. A request without a credential header, such as
  an anonymous page read, still goes over plain http when the address says so.
- The Cloudflare Access headers `CF-Access-Client-Id` and `CF-Access-Client-Secret` (from `SWARM_ACCESS_CLIENT_ID`
  and `SWARM_ACCESS_CLIENT_SECRET`) are not credential headers for the refusal. Keep `SWARM_URL` on `https://`
  whenever they are set.
- The rule judges the address the client resolves, not the network path. A loopback address forwarded elsewhere,
  such as an SSH tunnel, counts as loopback.
- The root `compose.yaml` stack does not set `LEDGER_TLS_CERT` or `LEDGER_TLS_KEY` on its `swarm` service and
  points its `controller` service at `LEDGER_URL: http://swarm:8765`. That host name resolves to a container
  address, which is not loopback, so the controller's credentialed requests are refused. Two changes are needed
  before they succeed: the `swarm` service serves TLS with a certificate that names `DNS:swarm`, and the
  controller's `LEDGER_URL` becomes `https://swarm:8765` with `LEDGER_CA_PEM` set to the signing CA.

## Evidence

- Delivered by [pull request 4351](https://github.com/the-cloud-clockwork/agentihooks/pull/4351), closing issue 4317.
- `tests/swarm_ledger/test_ledger_tls.py` covers the refusal for every sender, loopback and https sends, the https
  base and page origin, the both-or-neither rule, a credential reaching a real TLS ledger, a plain http client
  dropped quietly, the health check over TLS, CA trust for every sender and the handshake timeout.
- Red run on the test commit: <https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39082947351>.
  Green mutation preflight on the head: <https://github.com/the-cloud-clockwork/agentihooks/actions/runs/39087478563>.

## Related pages

- [Deployment modes](deployment-modes.md)
- [Hosted ledger auth](hosted-ledger-auth.md)
- [Hives](../pillars/hives.md)
