package dev.autonom.helper;

/**
 * An expected failure of one request. The code is part of protocol 1 and is
 * sent to the host as {@code error.code}; the message is for people.
 */
final class HelperError extends Exception {
    private static final long serialVersionUID = 1L;

    final String code;

    HelperError(String code, String message) {
        super(message);
        this.code = code;
    }

    static HelperError badRequest(String message) {
        return new HelperError("bad_request", message);
    }
}
