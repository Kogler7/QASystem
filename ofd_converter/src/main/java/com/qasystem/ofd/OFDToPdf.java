package com.qasystem.ofd;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import org.ofdrw.converter.export.OFDExporter;
import org.ofdrw.converter.export.PDFExporterPDFBox;

public final class OFDToPdf {
    private OFDToPdf() {}

    public static void main(String[] args) throws Exception {
        if (args.length != 2) {
            System.err.println("Usage: ofd-to-pdf <input.ofd> <output.pdf>");
            System.exit(2);
        }
        Path input = Paths.get(args[0]);
        Path output = Paths.get(args[1]);
        if (!Files.isRegularFile(input)) {
            throw new IllegalArgumentException("OFD input does not exist: " + input);
        }
        Path parent = output.toAbsolutePath().getParent();
        if (parent != null) {
            Files.createDirectories(parent);
        }
        try (OFDExporter exporter = new PDFExporterPDFBox(input, output)) {
            exporter.export();
        }
        if (!Files.isRegularFile(output) || Files.size(output) == 0) {
            throw new IllegalStateException("OFD conversion did not produce a PDF: " + output);
        }
    }
}
