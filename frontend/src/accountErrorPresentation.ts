export function isOAuthPhoneVerificationStopped(
  ...messages: Array<string | null | undefined>
): boolean {
  const text = messages.filter(Boolean).join(" ").toLowerCase();
  if (text.includes("oauth_phone_verification_stopped")) {
    return true;
  }

  const retriedOAuth = /重新\s*oauth/.test(text);
  const requestedPhoneVerification = text.includes("手机验证码") || text.includes("手机验证");
  const explicitlyStopped = text.includes("停止") || text.includes("终止");
  return retriedOAuth && requestedPhoneVerification && explicitlyStopped;
}
