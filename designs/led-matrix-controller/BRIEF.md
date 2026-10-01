Project: Driver Controller for LED Matrix Module System. Allowing for Ethernet controlling or directly via USB.

snippets from chat:

can this build an PCB that has USB PD also allows for UART over the same usb cable so both the usb ic and the uart can talk to the same allows to talk to an esp32-c6. also it should have support for these spi ethernet W5500 SPI thingys?

this would be the perfect controller/driver for the led modules

also it would need to follow the USB PD EPR 180 W spec. and maybe add an efuse 👉 👈

because its software defined this would allow me to make the led matrix module complety flexible
like did you plug it your old laptop? i see that because it doesn't give enough power i limit how many LEDs i can power up
got the full power? well here you go
do you have a standalone power brick? well i can receive UDP packages too!

writeup:

So im building a Modular LED Matrix controlled via touch designer.

Each Module is a 6x6 WS2812D-F8 connected in parallel.

Each Module has a Voltage Downstepper (XL1509-5.0E1)

allowing from putting down 40 V at 2 Amps to 5V

these models are chain.

Currently i just run these Modules via a lab powersupply but i want a nice controller board to it which allows for quick connecting to a laptop or when fully build just connected to a power supply and everything else is via ethernet

im not really an expert in anything and all PCB designs are currently done in EasyEDA Pro

and that controller board im planning is a bit too much to chew on for me
everything else for now was okay for now

like for IC i think of HUSB238A-BB001-QN16R 
but because of USB EPR it kinda get more complex


currently thats my led module v3 i made in easyeda:

https://cdn.discordapp.com/attachments/1542958352093413476/1543362944178323516/DXF_PCB6_2026-08-29_AutoCAD2007.dxf?ex=6abf7100&is=6abe1f80&hm=847964cb8225b96f1f3a857b75793c39887a88f19b3e7c6b3a0cb63c17ad12e4&

---

like i think i have the core ICs picked out, just need some plan on how to write everything. 

So the ESP32 can bootup (needs 3.3V rail). so the USB PD Chip can rase the voltage and somehow i want to use the USB 2.0 rail for a UART chip to the esp32-c6.
so thats kinda my most concern

i saw that the USB PD has some pinout for cutting power (like when the USB power supply overheats and shuts off) it can turn of the mosfet

https://cdn.discordapp.com/attachments/1542958352093413476/1543593779565691031/image.png?ex=6abf9f3b&is=6abe4dbb&hm=ad4670a7cd46b2b80bb703bda0d7387cfd28aca1490f82d353760dbf80720045

----

Currently i drive these modules with an ESP32-C6 Dev Kit + DC Power Supply, works good but not really portable or usable at a rave.